"""Test: large dataset (140 cols, up to 200K rows) reconciliation picks only needed columns.

Runs against the REAL PostgreSQL database (same as the app) to verify:
  1. load_frame() SELECTs only canonical columns from Postgres (not SELECT *)
  2. The returned DataFrame contains only the expected canonical columns
  3. Extra columns (135 of 140) are never loaded into Python memory
  4. Full reconciliation pipeline works correctly with wide datasets
  5. Performance is acceptable at 200K rows x 140 columns

Run all tests:        pytest tests/test_large_dataset_column_pruning.py -v
Run fast tests only:  pytest tests/test_large_dataset_column_pruning.py -v -m "not slow"
Run benchmark:        pytest tests/test_large_dataset_column_pruning.py -v -m slow
"""
from __future__ import annotations

import sys
import time
import uuid
from pathlib import Path

import pandas as pd
import pytest
from sqlalchemy import Column, Integer, MetaData, Table, Text, create_engine, event, text
from sqlalchemy.orm import Session, sessionmaker

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.core.enums import FileType
from app.db.base import Base
from app.db.load import (
    _SALES_CANONICALS,
    _SETTLE_CANONICALS,
    _resolve_needed_db_cols,
    load_frame,
)
from app.db.models import UploadedFile
from config.settings import get_settings


# ── PostgreSQL fixtures ──────────────────────────────────────────────────

@pytest.fixture(scope="session")
def pg_engine():
    """Real PostgreSQL engine from .env / settings."""
    eng = create_engine(get_settings().database_url, pool_pre_ping=True)
    # Ensure ORM tables exist (idempotent — skips existing)
    Base.metadata.create_all(eng)
    return eng


@pytest.fixture()
def pg_session(pg_engine):
    """Session that rolls back everything after each test (zero cleanup needed)."""
    conn = pg_engine.connect()
    txn = conn.begin()
    session = Session(bind=conn)
    yield session
    session.close()
    txn.rollback()
    conn.close()


# ── Helpers ──────────────────────────────────────────────────────────────

def _unique_table_name(prefix: str) -> str:
    """Generate a collision-free table name for test isolation."""
    return f"_test_{prefix}_{uuid.uuid4().hex[:8]}"


def _build_wide_table(
    session: Session,
    table_name: str,
    file_type: FileType,
    n_cols: int = 140,
    n_rows: int = 100,
) -> int:
    """Create a wide per-file table directly in PostgreSQL and return the file_id.

    Simulates what ``ingest_store.store_file_rows`` does, but without needing
    an actual Excel file — much faster for large row counts.
    """
    is_sales = file_type in {FileType.SALES_FORWARD, FileType.SALES_RT, FileType.SALES_RTO}

    # Canonical columns that the reconciliation engine cares about
    if is_sales:
        canonical_db_cols = {
            "packet_id": "Packet ID",
            "shipment_value": "Shipment Value",
            "sku_code": "SKU Code",
            "qty": "Qty",
            "transaction_date": "Order Created Date",
        }
    else:
        canonical_db_cols = {
            "order_release_id": "Order Release ID",
            "customer_paid_amt": "Customer Paid Amount",
            "packet_id": "Packet ID",
            "transaction_date": "Delivery Date",
        }

    # Build sanitized column names: canonical cols first, then extras
    sanitized_names: list[str] = list(canonical_db_cols.keys())
    original_headers: list[str] = list(canonical_db_cols.values())
    sanitized_to_original: dict[str, str] = dict(zip(sanitized_names, original_headers))

    extra_count = n_cols - len(canonical_db_cols)
    for i in range(extra_count):
        san = f"extra_col_{i:03d}"
        orig = f"Extra Col {i:03d}"
        sanitized_names.append(san)
        original_headers.append(orig)
        sanitized_to_original[san] = orig

    # Create the dynamic table using the session's connection
    metadata = MetaData()
    columns = [
        Column("row_id", Integer, primary_key=True, autoincrement=True),
        Column("row_index", Integer),
    ]
    for san in sanitized_names:
        columns.append(Column(san, Text))

    table = Table(table_name, metadata, *columns)
    metadata.create_all(session.connection())

    # Create UploadedFile record
    uf = UploadedFile(
        filename=f"{table_name}.xlsx",
        stored_path=f"/fake/{table_name}.xlsx",
        file_type=file_type.value,
        processing_status="COMPLETED",
        row_count=n_rows,
        column_headers={
            "table_name": table_name,
            "original_headers": original_headers,
            "sanitized_names": sanitized_names,
            "sanitized_to_original": sanitized_to_original,
        },
    )
    session.add(uf)
    session.flush()

    # Bulk-insert rows
    BATCH = 5000
    insert_rows: list[dict] = []
    for idx in range(n_rows):
        row: dict = {"row_index": idx}
        if is_sales:
            row["packet_id"] = f"PKT-{idx:06d}"
            row["shipment_value"] = str(round(100 + idx * 0.5, 2))
            row["sku_code"] = f"SKU-{idx % 50:03d}"
            row["qty"] = str(1 + idx % 10)
            row["transaction_date"] = "2024-02-15"
        else:
            row["order_release_id"] = f"OR-{idx:06d}"
            row["customer_paid_amt"] = str(round(100 + idx * 0.5, 2))
            row["packet_id"] = f"PKT-{idx:06d}"
            row["transaction_date"] = "2024-02-15"
        for i in range(extra_count):
            row[f"extra_col_{i:03d}"] = f"v{idx}_{i}"
        insert_rows.append(row)

    ins = table.insert()
    for i in range(0, len(insert_rows), BATCH):
        session.execute(ins, insert_rows[i : i + BATCH])
    session.flush()

    return uf.id


# ── SQL capture helper ───────────────────────────────────────────────────


class SQLCapture:
    """Context manager that records SQL statements executed on an engine."""

    def __init__(self, engine):
        self.engine = engine
        self.statements: list[str] = []

    def _listener(self, conn, cursor, statement, parameters, context, executemany):
        self.statements.append(statement)

    def __enter__(self):
        event.listen(self.engine, "before_cursor_execute", self._listener)
        return self

    def __exit__(self, *args):
        event.remove(self.engine, "before_cursor_execute", self._listener)

    def select_statements_for(self, table_name: str) -> list[str]:
        return [
            s for s in self.statements
            if table_name.lower() in s.lower() and s.strip().upper().startswith("SELECT")
        ]


# ═══════════════════════════════════════════════════════════════════════════
# UNIT TESTS — pure logic, no DB needed
# ═══════════════════════════════════════════════════════════════════════════


class TestResolveNeededDbCols:
    """Unit tests for the column-resolution helper."""

    def test_sales_forward_resolves_canonical_cols(self):
        headers_info = {
            "original_headers": [
                "Packet ID", "Shipment Value", "SKU Code", "Qty",
                "Order Created Date", "Extra A", "Extra B",
            ],
            "sanitized_to_original": {
                "packet_id": "Packet ID",
                "shipment_value": "Shipment Value",
                "sku_code": "SKU Code",
                "qty": "Qty",
                "order_created_date": "Order Created Date",
                "extra_a": "Extra A",
                "extra_b": "Extra B",
            },
        }
        cols = _resolve_needed_db_cols(headers_info, FileType.SALES_FORWARD)
        assert cols is not None
        assert "packet_id" in cols
        assert "shipment_value" in cols
        # Extra columns must NOT be included
        assert "extra_a" not in cols
        assert "extra_b" not in cols

    def test_settlement_forward_resolves_canonical_cols(self):
        headers_info = {
            "original_headers": [
                "Order Release ID", "Customer Paid Amount", "Packet ID",
                "Delivery Date", "Invoice Number", "Random Col",
            ],
            "sanitized_to_original": {
                "order_release_id": "Order Release ID",
                "customer_paid_amount": "Customer Paid Amount",
                "packet_id": "Packet ID",
                "delivery_date": "Delivery Date",
                "invoice_number": "Invoice Number",
                "random_col": "Random Col",
            },
        }
        cols = _resolve_needed_db_cols(headers_info, FileType.SETTLEMENT_FORWARD)
        assert cols is not None
        assert "order_release_id" in cols
        assert "customer_paid_amount" in cols
        assert "invoice_number" not in cols
        assert "random_col" not in cols

    def test_returns_none_when_headers_missing(self):
        assert _resolve_needed_db_cols({}, FileType.SALES_FORWARD) is None
        assert _resolve_needed_db_cols({"original_headers": []}, FileType.SALES_FORWARD) is None


# ═══════════════════════════════════════════════════════════════════════════
# POSTGRES INTEGRATION TESTS — column pruning verification
# ═══════════════════════════════════════════════════════════════════════════


class TestLoadFrameColumnPruning:
    """Verify load_frame SELECTs only needed columns from a 140-col Postgres table."""

    def test_sales_forward_140_cols_returns_only_canonical(self, pg_engine, pg_session):
        """140-column table -> load_frame returns only 5 sales canonical cols."""
        tbl = _unique_table_name("wide_sales")
        fid = _build_wide_table(pg_session, tbl, FileType.SALES_FORWARD, n_cols=140, n_rows=200)
        pg_session.flush()

        df = load_frame(pg_session, fid, FileType.SALES_FORWARD)

        # Only canonical sales columns should be present
        for col in _SALES_CANONICALS:
            assert col in df.columns, f"Missing canonical column: {col}"
        # No extra columns
        extras = [c for c in df.columns if c.startswith("extra") or c.startswith("Extra")]
        assert len(extras) == 0, f"Extra columns leaked into result: {extras}"
        assert len(df) == 200
        # Values must be correct
        assert df.iloc[0]["packet_id"] == "PKT-000000"
        assert float(df.iloc[0]["shipment_value"]) == 100.0

    def test_settlement_forward_140_cols_returns_only_canonical(self, pg_engine, pg_session):
        """140-column table -> load_frame returns only settlement canonical cols."""
        tbl = _unique_table_name("wide_settle")
        fid = _build_wide_table(pg_session, tbl, FileType.SETTLEMENT_FORWARD, n_cols=140, n_rows=200)
        pg_session.flush()

        df = load_frame(pg_session, fid, FileType.SETTLEMENT_FORWARD)

        for col in _SETTLE_CANONICALS:
            assert col in df.columns, f"Missing canonical column: {col}"
        extras = [c for c in df.columns if c.startswith("extra") or c.startswith("Extra")]
        assert len(extras) == 0, f"Extra columns leaked: {extras}"
        assert len(df) == 200

    def test_sql_is_pruned_not_select_star(self, pg_engine, pg_session):
        """Verify the actual SQL statement does NOT use SELECT *."""
        tbl = _unique_table_name("sql_check")
        fid = _build_wide_table(pg_session, tbl, FileType.SALES_FORWARD, n_cols=140, n_rows=10)
        pg_session.flush()

        with SQLCapture(pg_engine) as cap:
            load_frame(pg_session, fid, FileType.SALES_FORWARD)

        selects = cap.select_statements_for(tbl)
        assert len(selects) >= 1, "Expected at least one SELECT on the data table"

        for stmt in selects:
            assert "SELECT *" not in stmt.upper(), (
                f"Expected column-pruned query, got SELECT *:\n{stmt}"
            )
            # Should mention specific columns
            assert '"packet_id"' in stmt or "packet_id" in stmt

    def test_sql_selects_only_needed_columns_count(self, pg_engine, pg_session):
        """Verify SQL selects ~5 columns, not 140."""
        tbl = _unique_table_name("col_count")
        fid = _build_wide_table(pg_session, tbl, FileType.SALES_FORWARD, n_cols=140, n_rows=10)
        pg_session.flush()

        with SQLCapture(pg_engine) as cap:
            load_frame(pg_session, fid, FileType.SALES_FORWARD)

        selects = cap.select_statements_for(tbl)
        assert len(selects) >= 1
        # Count quoted column names in the SELECT clause
        select_clause = selects[0].split("FROM")[0]
        col_refs = select_clause.count('"')
        # Each column is quoted: "col" -> 2 quotes per col -> divide by 2
        n_selected = col_refs // 2
        assert n_selected <= 10, (
            f"Expected <=10 columns in SQL SELECT, got {n_selected}. "
            f"Query: {selects[0][:200]}"
        )

    def test_with_all_columns_uses_select_star(self, pg_engine, pg_session):
        """with_all_columns=True should use SELECT * (all 140 columns)."""
        tbl = _unique_table_name("all_cols")
        fid = _build_wide_table(pg_session, tbl, FileType.SALES_FORWARD, n_cols=140, n_rows=10)
        pg_session.flush()

        with SQLCapture(pg_engine) as cap:
            df = load_frame(pg_session, fid, FileType.SALES_FORWARD, with_all_columns=True)

        # Should have all columns (140 data + row_id/row_index may be dropped)
        assert len(df.columns) >= 100, f"Expected ~140 columns, got {len(df.columns)}"

        selects = cap.select_statements_for(tbl)
        assert any("*" in s for s in selects), "Expected SELECT * for with_all_columns=True"

    def test_values_are_correct_after_pruning(self, pg_engine, pg_session):
        """Column pruning must not corrupt data -- check several rows."""
        tbl = _unique_table_name("values")
        fid = _build_wide_table(pg_session, tbl, FileType.SALES_FORWARD, n_cols=140, n_rows=500)
        pg_session.flush()

        df = load_frame(pg_session, fid, FileType.SALES_FORWARD)

        # Row 0
        assert df.iloc[0]["packet_id"] == "PKT-000000"
        assert float(df.iloc[0]["shipment_value"]) == 100.0
        assert df.iloc[0]["sku_code"] == "SKU-000"
        # Row 99
        assert df.iloc[99]["packet_id"] == "PKT-000099"
        assert float(df.iloc[99]["shipment_value"]) == 149.5
        # Row 499 (last)
        assert df.iloc[499]["packet_id"] == "PKT-000499"
        assert len(df) == 500


class TestFullPipelineWithWideDataset:
    """End-to-end: create wide tables -> load_frame -> reconcile -> verify results."""

    def test_e2e_reconciliation_140_cols(self, pg_engine, pg_session):
        """Full pipeline: 140-col tables, run recon, verify matched results."""
        from app.services.run_service import run_reconciliation_from_db

        sf_tbl = _unique_table_name("e2e_sf")
        st_tbl = _unique_table_name("e2e_st")

        # Create sales forward: 140 cols, 500 rows
        sf_id = _build_wide_table(pg_session, sf_tbl, FileType.SALES_FORWARD, n_cols=140, n_rows=500)
        # Create settlement forward: 140 cols, 500 rows with matching packet_ids
        st_id = _build_wide_table(pg_session, st_tbl, FileType.SETTLEMENT_FORWARD, n_cols=140, n_rows=500)
        pg_session.flush()

        # Run reconciliation
        result = run_reconciliation_from_db(
            pg_session,
            sales_forward_file_id=sf_id,
            settlement_forward_file_id=st_id,
            reconciliation_month="2024-02",
            tolerance=1.0,
        )

        # Sales result should have canonical columns only
        assert "packet_id" in result.sales.columns
        assert "forward_shipment_value" in result.sales.columns
        assert "net_sales" in result.sales.columns
        assert len(result.sales) > 0

        # Settlement result should have canonical columns only
        assert "order_release_id" in result.settlement.columns
        assert "forward_customer_paid_amt" in result.settlement.columns
        assert len(result.settlement) > 0

        # Final result
        assert "status" in result.final.columns
        assert len(result.final) > 0

        # Summary cards populated
        assert result.summary_cards["net_sales"] > 0
        assert result.summary_cards["net_settlement"] > 0

        print(f"\n=== E2E 140-col RECONCILIATION ON POSTGRES PASSED ===")
        print(f"  Sales rows: {len(result.sales)}")
        print(f"  Settlement rows: {len(result.settlement)}")
        print(f"  Final rows: {len(result.final)}")
        print(f"  Matched: {result.summary_cards['matched_records']}")
        print(f"  Unmatched: {result.summary_cards['unmatched_records']}")


# ═══════════════════════════════════════════════════════════════════════════
# PERFORMANCE / STRESS TESTS  (mark with @pytest.mark.slow)
# ═══════════════════════════════════════════════════════════════════════════


@pytest.mark.slow
class TestPerformanceLargeDataset:
    """Benchmark tests with 200K rows x 140 columns on PostgreSQL.

    Run with: pytest tests/test_large_dataset_column_pruning.py -v -m slow -s
    """

    N_ROWS = 200_000
    N_COLS = 140

    @pytest.fixture()
    def wide_table(self, pg_engine, pg_session):
        """Create the 200K-row table once and return (session, file_id, table_name)."""
        tbl = _unique_table_name("perf")
        t0 = time.perf_counter()
        fid = _build_wide_table(
            pg_session, tbl, FileType.SALES_FORWARD,
            n_cols=self.N_COLS, n_rows=self.N_ROWS,
        )
        pg_session.flush()
        elapsed = time.perf_counter() - t0
        print(f"\n  Insert {self.N_ROWS:,} rows x {self.N_COLS} cols into Postgres: {elapsed:.2f}s")
        return pg_session, fid, tbl

    def test_load_frame_200k_rows_pruned_vs_full(self, pg_engine, wide_table):
        """200K rows x 140 cols: pruned load should be significantly faster."""
        session, fid, tbl = wide_table

        # Pruned load (should be fast — only ~5 columns)
        t1 = time.perf_counter()
        df_pruned = load_frame(session, fid, FileType.SALES_FORWARD)
        pruned_time = time.perf_counter() - t1

        assert len(df_pruned) == self.N_ROWS
        assert len(df_pruned.columns) <= len(_SALES_CANONICALS)
        print(f"  Pruned load ({len(df_pruned.columns)} cols): {pruned_time:.2f}s")

        # Full load (SELECT * — should be slower)
        t2 = time.perf_counter()
        df_all = load_frame(session, fid, FileType.SALES_FORWARD, with_all_columns=True)
        full_time = time.perf_counter() - t2

        assert len(df_all) == self.N_ROWS
        assert len(df_all.columns) >= 100
        print(f"  Full load ({len(df_all.columns)} cols): {full_time:.2f}s")

        # Pruned should be meaningfully faster than full
        speedup = full_time / pruned_time if pruned_time > 0 else float("inf")
        print(f"  Speedup: {speedup:.1f}x")
        assert speedup > 1.5, (
            f"Pruned load should be faster than full load. "
            f"Pruned={pruned_time:.2f}s, Full={full_time:.2f}s, Speedup={speedup:.1f}x"
        )

    def test_load_frame_200k_rows_data_integrity(self, pg_engine, wide_table):
        """200K rows: verify first, middle, and last rows have correct data."""
        session, fid, tbl = wide_table

        df = load_frame(session, fid, FileType.SALES_FORWARD)

        # First row
        assert df.iloc[0]["packet_id"] == "PKT-000000"
        assert float(df.iloc[0]["shipment_value"]) == 100.0

        # Middle row
        mid = self.N_ROWS // 2
        assert df.iloc[mid]["packet_id"] == f"PKT-{mid:06d}"
        expected_val = round(100 + mid * 0.5, 2)
        assert float(df.iloc[mid]["shipment_value"]) == expected_val

        # Last row
        last = self.N_ROWS - 1
        assert df.iloc[last]["packet_id"] == f"PKT-{last:06d}"

        print(f"\n  Data integrity verified for {self.N_ROWS:,} rows on Postgres")

    def test_memory_footprint_pruned_vs_full(self, pg_engine, wide_table):
        """Pruned DataFrame should use significantly less memory than full."""
        session, fid, tbl = wide_table

        df_pruned = load_frame(session, fid, FileType.SALES_FORWARD)
        mem_pruned = df_pruned.memory_usage(deep=True).sum() / (1024 * 1024)  # MB

        df_full = load_frame(session, fid, FileType.SALES_FORWARD, with_all_columns=True)
        mem_full = df_full.memory_usage(deep=True).sum() / (1024 * 1024)  # MB

        ratio = mem_full / mem_pruned if mem_pruned > 0 else float("inf")
        print(f"\n  Pruned memory: {mem_pruned:.1f} MB ({len(df_pruned.columns)} cols)")
        print(f"  Full memory:   {mem_full:.1f} MB ({len(df_full.columns)} cols)")
        print(f"  Memory ratio:  {ratio:.1f}x")

        # 140 cols vs ~5 cols -> memory should be at least 5x less
        assert ratio > 3, (
            f"Pruned DataFrame should use much less memory. "
            f"Pruned={mem_pruned:.1f}MB, Full={mem_full:.1f}MB, Ratio={ratio:.1f}x"
        )
