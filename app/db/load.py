"""Load stored rows from the per-file PostgreSQL tables as DataFrames.

Each uploaded file has its own table ``upload_data_{file_id}`` with real columns
(sanitized names).  This module reads from those tables, maps sanitized column
names back to original Excel headers, then applies the column_mapper to get
canonical names for the reconciliation engine.

**Optimization**: when ``with_all_columns=False`` (the default for reconciliation),
only the columns that map to canonical names are SELECTed from the database.
For a 140-column table where only 5 columns are needed, this avoids transferring
135 unused TEXT columns across the wire — a major saving on 200 K+ row datasets.
"""
from __future__ import annotations

import pandas as pd
from sqlalchemy import text
from sqlalchemy.orm import Session

from app.core.enums import FileType
from app.core.logging import get_logger
from app.db.models import UploadedFile
from app.ingestion.column_mapper import map_columns

log = get_logger(__name__)

_SALES = {FileType.SALES_FORWARD, FileType.SALES_RT, FileType.SALES_RTO}

# Canonical columns used by the reconciliation engine per category
_SALES_CANONICALS = ["packet_id", "shipment_value", "sku_code", "qty", "transaction_date"]
_SETTLE_CANONICALS = ["order_release_id", "customer_paid_amt", "packet_id", "transaction_date"]


def _resolve_needed_db_cols(headers_info: dict, file_type: FileType) -> list[str] | None:
    """Map canonical column needs back to sanitized DB column names.

    Returns a list of sanitized column names that correspond to the canonical
    columns required by the reconciliation stage, or ``None`` if the mapping
    cannot be determined (caller should fall back to ``SELECT *``).
    """
    original_headers = headers_info.get("original_headers", [])
    sanitized_to_orig = headers_info.get("sanitized_to_original", {})
    if not original_headers or not sanitized_to_orig:
        return None

    canonicals = set(_SALES_CANONICALS if file_type in _SALES else _SETTLE_CANONICALS)
    orig_to_sanitized = {v: k for k, v in sanitized_to_orig.items()}
    resolved = map_columns(original_headers, file_type)  # {original: canonical}

    needed: list[str] = []
    for orig, canon in resolved.items():
        if canon in canonicals:
            san = orig_to_sanitized.get(orig)
            if san:
                needed.append(san)

    return needed if needed else None


def load_frame(session: Session, file_id: int, file_type: FileType,
               with_all_columns: bool = False) -> pd.DataFrame:
    """Return a canonical-column DataFrame from the per-file table.

    Reads from the file's dedicated table (name stored in column_headers),
    maps sanitized column names back to original headers, then applies
    column_mapper for canonical renaming.

    When ``with_all_columns`` is False (default), only the DB columns that map
    to canonical names are fetched — this pushes column pruning into the SQL
    layer so PostgreSQL never reads or transfers unused columns.
    """
    # Get the header mapping stored at upload time
    uf = session.get(UploadedFile, file_id)
    if not uf or not uf.column_headers:
        canonicals = _SALES_CANONICALS if file_type in _SALES else _SETTLE_CANONICALS
        return pd.DataFrame(columns=canonicals)

    table_name = uf.column_headers.get("table_name", f"upload_data_{file_id}")
    headers_info = uf.column_headers
    sanitized_to_orig = headers_info.get("sanitized_to_original", {})

    # ── Build the SELECT statement ──────────────────────────────────────
    if with_all_columns:
        stmt = text(f'SELECT * FROM "{table_name}" ORDER BY row_index')
        log.debug("load_frame file=%s: SELECT * (all columns requested)", file_id)
    else:
        needed_cols = _resolve_needed_db_cols(headers_info, file_type)
        if needed_cols:
            cols_sql = ", ".join(f'"{c}"' for c in needed_cols)
            stmt = text(f'SELECT {cols_sql} FROM "{table_name}" ORDER BY row_index')
            log.info("load_frame file=%s: SELECT %d/%d columns (pruned)",
                     file_id, len(needed_cols),
                     len(headers_info.get("sanitized_names", [])))
        else:
            # Cannot determine needed columns — safe fallback
            stmt = text(f'SELECT * FROM "{table_name}" ORDER BY row_index')
            log.warning("load_frame file=%s: falling back to SELECT * "
                        "(could not resolve needed columns)", file_id)

    result = session.execute(stmt)
    rows = result.fetchall()
    col_names = list(result.keys())

    if not rows:
        canonicals = _SALES_CANONICALS if file_type in _SALES else _SETTLE_CANONICALS
        return pd.DataFrame(columns=canonicals)

    df = pd.DataFrame(rows, columns=col_names)

    # Drop internal columns (row_id, row_index) if present
    for c in ("row_id", "row_index"):
        if c in df.columns:
            df = df.drop(columns=[c])

    # Rename sanitized column names back to original Excel headers
    rename_to_orig = {}
    for san_name in df.columns:
        orig = sanitized_to_orig.get(san_name)
        if orig:
            rename_to_orig[san_name] = orig
    df = df.rename(columns=rename_to_orig)

    # Now apply column_mapper to get canonical names
    original_headers = list(df.columns)
    resolved = map_columns(original_headers, file_type)  # {original: canonical}
    df = df.rename(columns=resolved)

    if not with_all_columns:
        canonicals = _SALES_CANONICALS if file_type in _SALES else _SETTLE_CANONICALS
        keep = [c for c in canonicals if c in df.columns]
        df = df[keep]

    return df


def load_preview(session: Session, file_id: int, limit: int = 20) -> list[dict]:
    """Return the first ``limit`` rows from the per-file table for preview."""
    uf = session.get(UploadedFile, file_id)
    if not uf or not uf.column_headers:
        return []

    table_name = uf.column_headers.get("table_name", f"upload_data_{file_id}")
    headers_info = uf.column_headers
    sanitized_to_orig = headers_info.get("sanitized_to_original", {})

    stmt = text(f'SELECT * FROM "{table_name}" ORDER BY row_index LIMIT :lim')
    result = session.execute(stmt, {"lim": limit})
    rows = result.fetchall()
    col_names = list(result.keys())

    out = []
    for row in rows:
        d = {}
        for col_name, val in zip(col_names, row):
            if col_name in ("row_id",):
                continue
            # Use original header name for display
            display_name = sanitized_to_orig.get(col_name, col_name)
            d[display_name] = val
        out.append(d)
    return out


def file_has_rows(session: Session, file_id: int, file_type: FileType) -> bool:
    uf = session.get(UploadedFile, file_id)
    table_name = uf.column_headers.get("table_name", f"upload_data_{file_id}") if uf and uf.column_headers else f"upload_data_{file_id}"
    try:
        stmt = text(f'SELECT 1 FROM "{table_name}" LIMIT 1')
        result = session.execute(stmt).fetchone()
        return result is not None
    except Exception:
        return False
