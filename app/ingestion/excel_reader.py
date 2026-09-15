"""File ingestion optimized for large / binary spreadsheets.

Strategy (why this is fast even on a 300 MB .xlsb):
  * A `.xlsb`/`.xlsx` is decoded ONCE and cached as Parquet keyed on the file's
    path + modification time. The binary decode is the expensive step; after it,
    every read is a columnar Parquet read in milliseconds.
  * Reconciliation reads only the columns that map to canonical names — Parquet
    does true columnar reads, so ~130 unused columns cost nothing.
  * `iter_row_batches` streams the cache in row batches for bulk DB inserts when
    row counts are genuinely large.

Supported inputs: .xlsx, .xlsm, .xlsb, .xls. Engine is chosen by extension.
If pyarrow is unavailable the code degrades gracefully to a direct (uncached)
read; if pyxlsb is unavailable, .xlsb raises a clear, actionable error.
"""
from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Iterator

import pandas as pd

from app.core.enums import FileType
from app.core.logging import get_logger
from app.ingestion.column_mapper import map_columns, required_canonicals, resolution_report
from config.settings import get_settings

log = get_logger(__name__)

try:
    import pyarrow  # noqa: F401
    _HAVE_PARQUET = True
except Exception:  # noqa: BLE001
    _HAVE_PARQUET = False


class ExcelIngestError(Exception):
    pass


_ENGINES = {".xlsx": "openpyxl", ".xlsm": "openpyxl", ".xlsb": "pyxlsb", ".xls": "xlrd"}


def _engine_for(path: Path) -> str:
    ext = path.suffix.lower()
    if ext not in _ENGINES:
        raise ExcelIngestError(f"Unsupported file type '{ext}'. Use .xlsx, .xlsm, .xlsb or .xls.")
    engine = _ENGINES[ext]
    if engine == "pyxlsb":
        try:
            import pyxlsb  # noqa: F401
        except Exception as exc:  # noqa: BLE001
            raise ExcelIngestError(
                ".xlsb needs the 'pyxlsb' package. Install it: pip install pyxlsb"
            ) from exc
    return engine


def _sheet_names(path: Path) -> list[str]:
    with pd.ExcelFile(path, engine=_engine_for(path)) as xl:
        return xl.sheet_names


def _pick_sheet(path: Path, sheet: str | None) -> str:
    names = _sheet_names(path)
    if sheet and sheet in names:
        return sheet
    return names[0]


def read_headers(path: str | Path, sheet: str | None = None) -> list[str]:
    """Header row only — fast, reads no data rows."""
    path = Path(path)
    ws = _pick_sheet(path, sheet)
    df = pd.read_excel(path, sheet_name=ws, engine=_engine_for(path), nrows=0)
    return [str(c).strip() for c in df.columns]


# ---------------------------------------------------------------- Parquet cache
def _cache_path(path: Path, sheet: str) -> Path:
    cache_dir = get_settings().cache_dir
    cache_dir.mkdir(parents=True, exist_ok=True)
    key = f"{path.resolve()}|{sheet}|{path.stat().st_mtime_ns}"
    digest = hashlib.sha1(key.encode()).hexdigest()[:16]
    return cache_dir / f"{path.stem}.{digest}.parquet"


def _ensure_cache(path: Path, sheet: str) -> Path | None:
    """Decode the workbook once into Parquet; return the cache path (or None)."""
    if not _HAVE_PARQUET:
        return None
    cache = _cache_path(path, sheet)
    if cache.exists():
        return cache
    log.info("Decoding %s[%s] -> parquet cache (one-time)…", path.name, sheet)
    df = pd.read_excel(path, sheet_name=sheet, engine=_engine_for(path))
    df.columns = [str(c).strip() for c in df.columns]
    # store everything as-is; object columns to string for a stable parquet schema
    for c in df.columns:
        if df[c].dtype == "object":
            df[c] = df[c].astype("string")
    df.to_parquet(cache, index=False)
    log.info("Cached %s rows x %s cols", len(df), df.shape[1])
    return cache


def _apply_mapping(df: pd.DataFrame, file_type: FileType) -> pd.DataFrame:
    """Rename recognized headers to canonical names; leave all others untouched.

    Unrecognized columns keep their ORIGINAL names so any data-dependent check
    can reference them directly. Recon and the built-in checks use the canonical
    names for the fields they recognize.
    """
    resolved = map_columns(list(df.columns), file_type)
    return df.rename(columns=resolved)


def read_excel(
    path: str | Path, file_type: FileType, sheet: str | None = None,
    columns: list[str] | None = None,
) -> pd.DataFrame:
    """Return a DataFrame with ALL columns, recognized ones renamed to canonical.

    Default behavior keeps every column so any data-dependent check can reach
    any field; recognized headers are renamed to canonical names (recon uses
    those), and unrecognized columns keep their original names untouched.

    ``columns`` is an OPT-IN prune for a genuinely huge run: pass a list of
    canonical names to load only those (plus whatever else is needed). Leave it
    None (the default) to load everything. Either way a required column that
    cannot be resolved raises with a resolution report.
    """
    path = Path(path)
    if not path.exists():
        raise ExcelIngestError(f"File not found: {path}")
    ws = _pick_sheet(path, sheet)

    headers = read_headers(path, ws)
    resolved = map_columns(headers, file_type)
    missing = [c for c in required_canonicals(file_type) if c not in resolved.values()]
    if missing:
        rep = resolution_report(headers, file_type)
        hints = "; ".join(
            f"{canon}: try {[h for h, _ in cands]}" if cands else f"{canon}: no close match"
            for canon, cands in rep["suggestions"].items()
        )
        raise ExcelIngestError(
            f"Required columns not found for {file_type.value}: {missing}. "
            f"Suggestions -> {hints}. Add the correct source header as an alias "
            f"in app/ingestion/column_mapper.py COLUMN_MAPPINGS."
        )

    # Opt-in prune: translate requested canonical names back to raw headers.
    read_cols: list[str] | None = None
    if columns is not None:
        canon_to_raw = {v: k for k, v in resolved.items()}
        read_cols = [canon_to_raw.get(c, c) for c in columns]
        read_cols = [c for c in read_cols if c in headers]

    cache = _ensure_cache(path, ws)
    if cache is not None:
        df = pd.read_parquet(cache, columns=read_cols)   # None => all columns
    else:  # no pyarrow: direct read (still correct, just not cached)
        df = pd.read_excel(path, sheet_name=ws, engine=_engine_for(path), usecols=read_cols)
        df.columns = [str(c).strip() for c in df.columns]

    log.info("Loaded %s rows x %s cols from %s[%s] (mapped=%s, pruned=%s)",
             len(df), df.shape[1], path.name, ws, len(resolved), columns is not None)
    return _apply_mapping(df, file_type)


def iter_row_batches(
    path: str | Path, file_type: FileType, sheet: str | None = None,
    chunk_rows: int | None = None, columns: list[str] | None = None,
) -> Iterator[pd.DataFrame]:
    """Stream the cached data in row batches (for bulk DB insert on big files).

    Loads all columns by default; pass ``columns`` (canonical names) to prune.
    """
    path = Path(path)
    chunk_rows = chunk_rows or get_settings().excel_chunk_rows
    ws = _pick_sheet(path, sheet)
    resolved = map_columns(read_headers(path, ws), file_type)
    cache = _ensure_cache(path, ws)

    read_cols: list[str] | None = None
    if columns is not None:
        canon_to_raw = {v: k for k, v in resolved.items()}
        read_cols = [canon_to_raw.get(c, c) for c in columns]

    if cache is not None and _HAVE_PARQUET:
        import pyarrow.parquet as pq
        pf = pq.ParquetFile(cache)
        for batch in pf.iter_batches(batch_size=chunk_rows, columns=read_cols):
            yield _apply_mapping(batch.to_pandas(), file_type)
    else:
        df = read_excel(path, file_type, ws, columns=columns)
        for i in range(0, len(df), chunk_rows):
            yield df.iloc[i:i + chunk_rows]
