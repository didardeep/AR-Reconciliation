"""Verify that every column from the original spreadsheet survives storage.

Creates a 150-column .xlsx with blanks scattered in the middle, stores it via
the ingest pipeline into a temp SQLite database, reads a row back, and asserts
the JSONB ``data`` has ALL 150 original headers with values under the CORRECT
headers (proves no column shift from sparse-row handling).
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pandas as pd
import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session, sessionmaker

# ensure project root is importable
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.db.base import Base
from app.db.models import RawRecord, UploadedFile
from app.core.enums import FileType
from app.db.ingest_store import store_file_rows


def _make_150col_xlsx(path: Path) -> tuple[list[str], dict]:
    """Build a 150-column xlsx with blanks in the middle.

    Returns (headers, expected_values_for_row_0).
    """
    headers = []
    row_values = {}
    for i in range(150):
        h = f"Col_{i:03d}"
        headers.append(h)
        # Columns 0-1 are the required canonical columns (mapped)
        if i == 0:
            h_name = "Packet ID"
            headers[-1] = h_name
            row_values[h_name] = "PKT-999"
        elif i == 1:
            h_name = "Shipment Value"
            headers[-1] = h_name
            row_values[h_name] = 42.5
        # Scatter blanks at positions 10, 30, 50, 70, 90, 110, 130
        elif i in (10, 30, 50, 70, 90, 110, 130):
            row_values[h] = None
        else:
            row_values[h] = f"val_{i}"

    df = pd.DataFrame([row_values], columns=headers)
    df.to_excel(path, index=False)
    return headers, row_values


def test_faithful_150_column_storage(tmp_path):
    """Store a 150-col xlsx and verify every header/value survives intact."""
    xlsx_path = tmp_path / "wide_test.xlsx"
    headers, expected = _make_150col_xlsx(xlsx_path)
    assert len(headers) == 150, f"Test setup: expected 150 headers, got {len(headers)}"

    # Create a temp SQLite DB
    db_path = tmp_path / "test.db"
    engine = create_engine(f"sqlite:///{db_path}", echo=False)
    Base.metadata.create_all(engine)
    SessionLocal = sessionmaker(bind=engine)

    with SessionLocal() as session:
        # Create UploadedFile record
        uf = UploadedFile(
            filename="wide_test.xlsx",
            stored_path=str(xlsx_path),
            file_type=FileType.SALES_FORWARD.value,
            processing_status="PARSING",
        )
        session.add(uf)
        session.flush()

        # Store rows
        row_count = store_file_rows(session, uf.id, FileType.SALES_FORWARD, str(xlsx_path))
        session.commit()

        assert row_count == 1

        # Read the stored row back
        raw = session.execute(
            select(RawRecord).where(RawRecord.file_id == uf.id)
        ).scalars().first()

        assert raw is not None
        data = raw.data
        assert isinstance(data, dict)

        # Print the stored column count for verification
        stored_cols = len(data)
        print(f"\n=== STORED COLUMN COUNT: {stored_cols} ===")
        print(f"=== ORIGINAL HEADERS: {len(headers)} ===")

        # ALL 150 original headers must be present in data
        assert stored_cols == 150, (
            f"Expected 150 columns in stored data, got {stored_cols}. "
            f"Missing: {set(headers) - set(data.keys())}"
        )

        # Every header must be present
        for h in headers:
            assert h in data, f"Original header '{h}' missing from stored data"

        # Values must be under the CORRECT headers (no shift)
        assert data["Packet ID"] == "PKT-999", f"Packet ID shifted: got {data['Packet ID']}"
        assert data["Shipment Value"] == 42.5, f"Shipment Value shifted: got {data['Shipment Value']}"

        # Check a sample of non-blank columns have correct values
        for i in [5, 20, 40, 60, 80, 100, 120, 140]:
            h = f"Col_{i:03d}"
            assert data[h] == f"val_{i}", f"Column {h} has wrong value: {data[h]} (expected val_{i})"

        # Check blank columns are None
        for i in [10, 30, 50, 70, 90, 110, 130]:
            h = f"Col_{i:03d}"
            assert data[h] is None, f"Blank column {h} should be None, got {data[h]}"

        # Verify helper columns were populated correctly
        assert raw.packet_id == "PKT-999"
        assert raw.shipment_value == 42.5

        print("=== ALL 150 COLUMNS VERIFIED: NO SHIFT ===")


def test_load_frame_returns_canonical_columns(tmp_path):
    """Verify load_frame returns canonical columns for reconciliation."""
    from app.db.load import load_frame

    xlsx_path = tmp_path / "small.xlsx"
    pd.DataFrame({
        "Packet ID": ["PKT-1", "PKT-2"],
        "Shipment Value": [100.0, 200.0],
        "Extra Column": ["a", "b"],
    }).to_excel(xlsx_path, index=False)

    db_path = tmp_path / "test_load.db"
    engine = create_engine(f"sqlite:///{db_path}", echo=False)
    Base.metadata.create_all(engine)
    SessionLocal = sessionmaker(bind=engine)

    with SessionLocal() as session:
        uf = UploadedFile(
            filename="small.xlsx", stored_path=str(xlsx_path),
            file_type=FileType.SALES_FORWARD.value, processing_status="PARSING",
        )
        session.add(uf)
        session.flush()
        store_file_rows(session, uf.id, FileType.SALES_FORWARD, str(xlsx_path))
        session.commit()

        # Default: only canonical columns
        df = load_frame(session, uf.id, FileType.SALES_FORWARD)
        assert "packet_id" in df.columns
        assert "shipment_value" in df.columns
        assert "Extra Column" not in df.columns

        # with_all_columns: all original columns plus canonical renames
        df_all = load_frame(session, uf.id, FileType.SALES_FORWARD, with_all_columns=True)
        assert "packet_id" in df_all.columns
        assert "shipment_value" in df_all.columns
        assert "Extra Column" in df_all.columns
        assert len(df_all) == 2
