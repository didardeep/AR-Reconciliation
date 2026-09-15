"""Month-based checks.

Validates a date column against the user-selected reconciliation month and
returns counts (in-month / out-of-month / missing / invalid). Out-of-month rows
are flagged for investigation, never removed.
"""
from __future__ import annotations

from dataclasses import dataclass

import pandas as pd

from app.core.enums import ExceptionCode, Severity
from app.exceptions_framework import ExceptionCollector


@dataclass
class MonthCheckResult:
    total: int
    in_month: int
    out_of_month: int
    missing_date: int
    invalid_date: int


def _parse_dates(series: pd.Series) -> pd.Series:
    # accept YYYYMMDD ints, ISO strings, and datetimes
    s = series.copy()
    as_str = s.astype(str).str.strip()
    numeric_yyyymmdd = as_str.str.fullmatch(r"\d{8}")
    out = pd.to_datetime(as_str.where(~numeric_yyyymmdd), errors="coerce")
    out2 = pd.to_datetime(as_str.where(numeric_yyyymmdd), format="%Y%m%d", errors="coerce")
    return out.fillna(out2)


def check_month(
    df: pd.DataFrame, id_col: str, date_col: str, year: int, month: int,
    source_file: str, collector: ExceptionCollector,
) -> MonthCheckResult:
    if date_col not in df.columns:
        return MonthCheckResult(len(df), 0, 0, len(df), 0)

    dates = _parse_dates(df[date_col])
    missing = df[date_col].isna() | (df[date_col].astype(str).str.strip() == "")
    invalid = dates.isna() & ~missing
    in_month = (dates.dt.year == year) & (dates.dt.month == month)
    out_month = dates.notna() & ~in_month

    for ref in df.index[missing]:
        collector.add(ExceptionCode.MISSING_DATE, df.at[ref, id_col] if id_col in df else f"row:{ref}",
                      source_file, f"Missing {date_col}", severity=Severity.WARNING, stage="month")
    for ref in df.index[invalid]:
        collector.add(ExceptionCode.INVALID_DATE, df.at[ref, id_col] if id_col in df else f"row:{ref}",
                      source_file, f"Unparseable {date_col}: {df.at[ref, date_col]!r}",
                      severity=Severity.ERROR, stage="month")
    for ref in df.index[out_month]:
        collector.add(ExceptionCode.MONTH_MISMATCH, df.at[ref, id_col] if id_col in df else f"row:{ref}",
                      source_file, f"{date_col} outside {year}-{month:02d}",
                      severity=Severity.WARNING, stage="month",
                      expected_value=f"{year}-{month:02d}", actual_value=str(dates[ref].date()),
                      transaction_date=str(dates[ref].date()))

    return MonthCheckResult(
        total=len(df), in_month=int(in_month.sum()), out_of_month=int(out_month.sum()),
        missing_date=int(missing.sum()), invalid_date=int(invalid.sum()),
    )
