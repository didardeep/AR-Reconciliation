"""File checks + data-quality checks -> Data Quality Summary.

Runs before reconciliation. File checks confirm the workbook is usable and the
required canonical columns resolved; data checks profile missing/duplicate IDs,
invalid amounts and blank rows/cols. Nothing is dropped — issues are recorded
on an ExceptionCollector and summarised.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

import pandas as pd

from app.core.enums import ExceptionCode, FileType, Severity
from app.exceptions_framework import ExceptionCollector
from app.ingestion.column_mapper import map_columns, required_canonicals
from app.ingestion.excel_reader import read_headers


@dataclass
class DataQualitySummary:
    source_file: str
    file_type: str
    total_rows: int = 0
    total_columns: int = 0
    resolved_columns: list[str] = field(default_factory=list)
    missing_required: list[str] = field(default_factory=list)
    blank_rows: int = 0
    blank_columns: int = 0
    missing_id: int = 0
    duplicate_id: int = 0
    missing_amount: int = 0
    invalid_amount: int = 0
    passed: bool = True

    def to_dict(self) -> dict:
        return self.__dict__


def check_file_readable(path: str | Path, file_type: FileType) -> tuple[bool, list[str]]:
    """Return (ok, missing_required_canonicals). Fast — reads headers only."""
    headers = read_headers(Path(path))
    resolved = set(map_columns(headers, file_type).values())
    missing = [c for c in required_canonicals(file_type) if c not in resolved]
    return (len(missing) == 0, missing)


def _id_column(file_type: FileType) -> str:
    return "order_release_id" if file_type.value.startswith("settlement") else "packet_id"


def _amount_column(file_type: FileType) -> str:
    return "customer_paid_amt" if file_type.value.startswith("settlement") else "shipment_value"


def profile(
    df: pd.DataFrame, file_type: FileType, source_file: str, collector: ExceptionCollector
) -> DataQualitySummary:
    s = DataQualitySummary(
        source_file=source_file,
        file_type=file_type.value,
        total_rows=len(df),
        total_columns=df.shape[1],
        resolved_columns=[c for c in df.columns if not c.startswith("raw__")],
    )

    ok, missing = True, [c for c in required_canonicals(file_type) if c not in df.columns]
    s.missing_required = missing
    if missing:
        s.passed = False
        collector.add(
            ExceptionCode.INVALID_AMOUNT, source_file, source_file,
            f"Required columns missing after mapping: {missing}",
            severity=Severity.ERROR, stage="quality",
        )
        return s

    if len(df):
        s.blank_rows = int(df.isna().all(axis=1).sum())
        s.blank_columns = int(df.isna().all(axis=0).sum())

    id_col, amt_col = _id_column(file_type), _amount_column(file_type)

    missing_id_mask = df[id_col].isna() | (df[id_col].astype(str).str.strip() == "")
    s.missing_id = int(missing_id_mask.sum())
    for ref in df.index[missing_id_mask]:
        collector.add(
            ExceptionCode.MISSING_PACKET_ID if id_col == "packet_id"
            else ExceptionCode.MISSING_ORDER_RELEASE_ID,
            f"row:{ref}", source_file, f"Missing {id_col}",
            severity=Severity.ERROR, stage="quality",
        )

    present = df.loc[~missing_id_mask, id_col].astype(str).str.strip()
    dup_ids = present[present.duplicated(keep=False)].unique()
    s.duplicate_id = int(len(dup_ids))
    # (duplicates are aggregated in recon; recorded here only as INFO for visibility)
    for did in dup_ids[:1000]:
        collector.add(
            ExceptionCode.DUPLICATE_PACKET_ID if id_col == "packet_id"
            else ExceptionCode.DUPLICATE_ORDER_RELEASE_ID,
            did, source_file, f"Duplicate {id_col} (will be aggregated)",
            severity=Severity.INFO, stage="quality",
        )

    amt = pd.to_numeric(df[amt_col], errors="coerce")
    s.missing_amount = int(df[amt_col].isna().sum())
    s.invalid_amount = int((amt.isna() & df[amt_col].notna()).sum())
    for ref in df.index[amt.isna() & df[amt_col].notna()]:
        collector.add(
            ExceptionCode.INVALID_SHIPMENT_VALUE if amt_col == "shipment_value"
            else ExceptionCode.INVALID_AMOUNT,
            f"row:{ref}", source_file, f"Non-numeric {amt_col}: {df.at[ref, amt_col]!r}",
            severity=Severity.ERROR, stage="quality",
        )

    return s
