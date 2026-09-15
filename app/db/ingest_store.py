"""Persist an uploaded file's rows into a dedicated PostgreSQL table.

Each uploaded file gets its OWN table named ``upload_data_{file_id}`` with real
columns matching the exact Excel headers.  You can browse the data in pgAdmin
just like a spreadsheet — every column, every row, exactly as uploaded.

Column names are sanitized to be valid PostgreSQL identifiers (lowercased,
spaces/special chars replaced with underscores, de-duplicated).  The original
header list is stored on ``UploadedFile.column_headers`` so the load path can
map sanitized names back to originals for column_mapper resolution.
"""
from __future__ import annotations

import math
import re
import time
from pathlib import Path

import pandas as pd
from sqlalchemy import Column, Integer, MetaData, String, Table, Text, text
from sqlalchemy.orm import Session

from app.core.enums import FileType
from app.core.logging import get_logger
from app.db.session import engine as get_engine
from app.ingestion.excel_reader import read_excel, read_headers

log = get_logger(__name__)

_BATCH = 5000


# ── column name sanitization ─────────────────────────────────────────

def _sanitize_col(name: str) -> str:
    """Turn an Excel header into a valid PostgreSQL column name."""
    s = str(name).strip()
    if not s:
        s = "unnamed"
    # lowercase, replace non-alphanumeric with underscore
    s = re.sub(r"[^a-z0-9]", "_", s.lower())
    # collapse multiple underscores
    s = re.sub(r"_+", "_", s).strip("_")
    # must start with a letter or underscore
    if s and s[0].isdigit():
        s = f"c_{s}"
    if not s:
        s = "col"
    return s


def _unique_columns(headers: list[str]) -> tuple[list[str], dict[str, str]]:
    """Return (sanitized_names, {sanitized: original}) with no duplicates.

    If two Excel headers sanitize to the same name, a numeric suffix is added.
    """
    sanitized_list: list[str] = []
    sanitized_to_orig: dict[str, str] = {}
    seen: dict[str, int] = {}

    for orig in headers:
        base = _sanitize_col(orig)
        if base in seen:
            seen[base] += 1
            col_name = f"{base}_{seen[base]}"
        else:
            seen[base] = 1
            col_name = base

        sanitized_list.append(col_name)
        sanitized_to_orig[col_name] = orig

    return sanitized_list, sanitized_to_orig


# ── value helpers ─────────────────────────────────────────────────────

def _safe_str(v) -> str | None:
    """Convert any value to a string safe for TEXT columns, or None."""
    if v is None:
        return None
    if isinstance(v, float) and math.isnan(v):
        return None
    if isinstance(v, pd.Timestamp):
        return None if pd.isna(v) else v.isoformat()
    try:
        if pd.isna(v):
            return None
    except (TypeError, ValueError):
        pass
    s = str(v).strip()
    return s if s else None


def _table_name_from_filename(filename: str) -> str:
    """Derive a PostgreSQL table name from the uploaded filename.

    Example: 'Dummy_Sales_Forward.xlsx' -> 'dummy_sales_forward'
    """
    name = Path(filename).stem  # drop extension
    sanitized = re.sub(r"[^a-z0-9]", "_", name.lower())
    sanitized = re.sub(r"_+", "_", sanitized).strip("_")
    if sanitized and sanitized[0].isdigit():
        sanitized = f"t_{sanitized}"
    return sanitized or "upload_data"


# ── main entry point ──────────────────────────────────────────────────

def store_file_rows(session: Session, file_id: int, file_type: FileType, path: str) -> int:
    """Parse the file and create a dedicated table with real columns.

    The table is named after the file (e.g. ``dummy_sales_forward``).  It has:
      - ``row_id``   (serial PK)
      - ``row_index`` (integer, 0-based)
      - one TEXT column per Excel header (sanitized name)

    The original headers, sanitized mapping, and table name are stored on the
    ``UploadedFile`` record so load.py can find the table and reverse the mapping.
    """
    df = read_excel(path, file_type)
    original_headers = read_headers(path)

    # Build sanitized column names
    sanitized_names, sanitized_to_orig = _unique_columns(original_headers)

    # The df returned by read_excel has canonical names for recognized columns
    # and original names for unresolved ones.  We need to map df columns back
    # to the sanitized names.
    from app.ingestion.column_mapper import map_columns
    orig_to_canon = map_columns(original_headers, file_type)  # {orig: canon}
    canon_to_orig = {v: k for k, v in orig_to_canon.items()}  # {canon: orig}

    # Build df_col -> sanitized_name mapping
    # df columns are either canonical (for resolved) or original (for unresolved)
    orig_to_sanitized = {}
    for san, orig in sanitized_to_orig.items():
        orig_to_sanitized[orig] = san

    df_col_to_sanitized: dict[str, str] = {}
    for col in df.columns:
        if col in canon_to_orig:
            # This is a canonical name -> find the original -> find sanitized
            orig = canon_to_orig[col]
            df_col_to_sanitized[col] = orig_to_sanitized.get(orig, _sanitize_col(col))
        else:
            # Unresolved column — already original name
            df_col_to_sanitized[col] = orig_to_sanitized.get(col, _sanitize_col(col))

    n = len(df)
    table_name = _table_name_from_filename(Path(path).name)
    log.info("Creating table %s with %s columns for %s rows…", table_name, len(sanitized_names), n)

    eng = get_engine()

    # Drop table if exists (re-upload scenario)
    with eng.begin() as conn:
        conn.execute(text(f'DROP TABLE IF EXISTS "{table_name}" CASCADE'))

    # Create the table dynamically
    metadata = MetaData()
    columns = [
        Column("row_id", Integer, primary_key=True, autoincrement=True),
        Column("row_index", Integer),
    ]
    for san_name in sanitized_names:
        columns.append(Column(san_name, Text))

    table = Table(table_name, metadata, *columns)
    metadata.create_all(eng)

    # Store the header mapping + table name on the UploadedFile record
    from app.db.models import UploadedFile
    uf = session.get(UploadedFile, file_id)
    if uf:
        uf.column_headers = {
            "table_name": table_name,
            "original_headers": original_headers,
            "sanitized_names": sanitized_names,
            "sanitized_to_original": sanitized_to_orig,
        }

    # Build insert rows
    insert_rows: list[dict] = []
    for row_idx, rec in enumerate(df.to_dict(orient="records")):
        row_dict: dict = {"row_index": row_idx}
        for df_col, val in rec.items():
            san_name = df_col_to_sanitized.get(df_col)
            if san_name:
                row_dict[san_name] = _safe_str(val)
        insert_rows.append(row_dict)

    # Bulk insert in batches
    ins = table.insert()
    for i in range(0, len(insert_rows), _BATCH):
        batch_start = time.perf_counter()
        batch = insert_rows[i:i + _BATCH]
        session.execute(ins, batch)
        elapsed = time.perf_counter() - batch_start
        log.info("  batch %s–%s (%s rows) inserted in %.2fs",
                 i, i + len(batch), len(batch), elapsed)
    session.flush()
    log.info("Stored %s rows in table %s", n, table_name)
    return n


def delete_file_rows(session: Session, file_id: int, file_type: FileType) -> None:
    """Drop the data table for a file (re-upload / re-store)."""
    from app.db.models import UploadedFile
    uf = session.get(UploadedFile, file_id)
    table_name = None
    if uf and uf.column_headers:
        table_name = uf.column_headers.get("table_name")
    if not table_name:
        table_name = f"upload_data_{file_id}"  # fallback
    eng = get_engine()
    with eng.begin() as conn:
        conn.execute(text(f'DROP TABLE IF EXISTS "{table_name}" CASCADE'))
