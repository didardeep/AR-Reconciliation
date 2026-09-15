"""End-to-end test: store Excel → run_reconciliation_from_db → verify results.

Exercises the full pipeline through a temp SQLite database:
  1. Create sample sales_forward, settlement_forward xlsx files
  2. Store them via store_file_rows (faithful JSONB storage)
  3. Run reconciliation from DB
  4. Assert results are correct (matched/unmatched counts, net values)
"""
from __future__ import annotations

import sys
from pathlib import Path

import pandas as pd
import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.db.base import Base
from app.db.models import RawRecord, UploadedFile
from app.core.enums import FileType
from app.db.ingest_store import store_file_rows
from app.services.run_service import run_reconciliation_from_db


def _make_sales_forward(path: Path):
    pd.DataFrame({
        "Packet ID": ["PKT-001", "PKT-001", "PKT-002", "PKT-003"],
        "Shipment Value": [500, 300, 400, 600],
        "Extra Sales Col": ["x", "y", "z", "w"],
    }).to_excel(path, index=False)


def _make_settlement_forward(path: Path):
    pd.DataFrame({
        "Order Release ID": ["OR-001", "OR-002", "OR-003"],
        "Customer Paid Amount": [800, 400, 600],
        "Packet ID": ["PKT-001", "PKT-002", "PKT-003"],
        "Extra Settle Col": ["a", "b", "c"],
    }).to_excel(path, index=False)


def test_e2e_store_and_reconcile(tmp_path):
    """Full pipeline: store xlsx → reconcile from DB → verify results."""
    sales_path = tmp_path / "sales_forward.xlsx"
    settle_path = tmp_path / "settlement_forward.xlsx"
    _make_sales_forward(sales_path)
    _make_settlement_forward(settle_path)

    db_path = tmp_path / "e2e_test.db"
    engine = create_engine(f"sqlite:///{db_path}", echo=False)
    Base.metadata.create_all(engine)
    SessionLocal = sessionmaker(bind=engine)

    with SessionLocal() as session:
        # Store sales forward
        uf_sales = UploadedFile(
            filename="sales_forward.xlsx", stored_path=str(sales_path),
            file_type=FileType.SALES_FORWARD.value, processing_status="PARSING",
        )
        session.add(uf_sales)
        session.flush()
        n_sales = store_file_rows(session, uf_sales.id, FileType.SALES_FORWARD, str(sales_path))
        assert n_sales == 4

        # Store settlement forward
        uf_settle = UploadedFile(
            filename="settlement_forward.xlsx", stored_path=str(settle_path),
            file_type=FileType.SETTLEMENT_FORWARD.value, processing_status="PARSING",
        )
        session.add(uf_settle)
        session.flush()
        n_settle = store_file_rows(session, uf_settle.id, FileType.SETTLEMENT_FORWARD, str(settle_path))
        assert n_settle == 3

        session.commit()

        # Verify JSONB data stored faithfully (original headers preserved)
        raw_sales = session.query(RawRecord).filter_by(file_id=uf_sales.id).first()
        assert "Packet ID" in raw_sales.data, "Original header 'Packet ID' must be in data"
        assert "Shipment Value" in raw_sales.data, "Original header 'Shipment Value' must be in data"
        assert "Extra Sales Col" in raw_sales.data, "Original header 'Extra Sales Col' must be in data"

        raw_settle = session.query(RawRecord).filter_by(file_id=uf_settle.id).first()
        assert "Order Release ID" in raw_settle.data
        assert "Customer Paid Amount" in raw_settle.data
        assert "Extra Settle Col" in raw_settle.data

        # Run reconciliation from DB
        result = run_reconciliation_from_db(
            session,
            sales_forward_file_id=uf_sales.id,
            settlement_forward_file_id=uf_settle.id,
            reconciliation_month="2024-02",
            tolerance=1.0,
        )

        # Sales recon: PKT-001 has 800 (500+300), PKT-002 has 400, PKT-003 has 600
        sales_df = result.sales.set_index("packet_id")
        assert sales_df.loc["PKT-001", "forward_shipment_value"] == 800
        assert sales_df.loc["PKT-002", "forward_shipment_value"] == 400
        assert sales_df.loc["PKT-003", "forward_shipment_value"] == 600

        # Settlement recon: OR-001=800, OR-002=400, OR-003=600
        settle_df = result.settlement.set_index("order_release_id")
        assert settle_df.loc["OR-001", "forward_customer_paid_amt"] == 800
        assert settle_df.loc["OR-002", "forward_customer_paid_amt"] == 400
        assert settle_df.loc["OR-003", "forward_customer_paid_amt"] == 600

        # Final: settlement has packet_id -> mapping established
        # PKT-001↔OR-001 matched (800-800=0), PKT-002↔OR-002 matched (400-400=0),
        # PKT-003↔OR-003 matched (600-600=0)
        final_df = result.final
        matched = final_df[final_df["status"] == "MATCHED"]
        assert len(matched) == 3, f"Expected 3 matched, got {len(matched)}"

        # Summary cards
        assert result.summary_cards["matched_records"] == 3
        assert result.summary_cards["net_sales"] == 1800.0  # 800+400+600
        assert result.summary_cards["net_settlement"] == 1800.0  # 800+400+600
        assert result.summary_cards["difference"] == 0.0

        print(f"\n=== E2E TEST PASSED ===")
        print(f"  Sales rows stored: {n_sales}")
        print(f"  Settlement rows stored: {n_settle}")
        print(f"  Matched records: {result.summary_cards['matched_records']}")
        print(f"  Net difference: {result.summary_cards['difference']}")
