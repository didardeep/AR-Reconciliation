"""Exception framework.

Every reconciliation stage records issues here rather than silently dropping
rows. An ``ExceptionRecord`` is a structured, exportable object; the
``ExceptionCollector`` aggregates them and can emit a per-type summary for the
dashboard.
"""
from __future__ import annotations

from dataclasses import dataclass, field, asdict
from typing import Any

import pandas as pd

from app.core.enums import ExceptionCode, Severity


@dataclass
class ExceptionRecord:
    code: ExceptionCode
    severity: Severity
    stage: str                       # "sales" | "settlement" | "final" | "quality" | "sku" | "month"
    record_id: str                   # packet_id / order_release_id / row ref
    source_file: str
    description: str
    expected_value: Any = None
    actual_value: Any = None
    difference: float | None = None
    transaction_date: str | None = None

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["code"] = self.code.value
        d["severity"] = self.severity.value
        return d


@dataclass
class ExceptionCollector:
    stage_default: str = "generic"
    cap_per_code: int = 5000          # max stored records per code (memory/DB bound)
    _records: list[ExceptionRecord] = field(default_factory=list)
    _counts: dict[str, int] = field(default_factory=dict)          # true totals per code
    _noninfo_total: int = 0                                        # true non-INFO total

    def add(
        self,
        code: ExceptionCode,
        record_id: Any,
        source_file: str,
        description: str,
        *,
        severity: Severity = Severity.WARNING,
        stage: str | None = None,
        expected_value: Any = None,
        actual_value: Any = None,
        difference: float | None = None,
        transaction_date: Any = None,
    ) -> None:
        key = code.value
        self._counts[key] = self._counts.get(key, 0) + 1
        if severity != Severity.INFO:
            self._noninfo_total += 1
        # Cap stored records per code; totals above are still counted for summary.
        if self._counts[key] > self.cap_per_code:
            return
        self._records.append(
            ExceptionRecord(
                code=code,
                severity=severity,
                stage=stage or self.stage_default,
                record_id=str(record_id),
                source_file=source_file,
                description=description,
                expected_value=expected_value,
                actual_value=actual_value,
                difference=difference,
                transaction_date=None if transaction_date is None else str(transaction_date),
            )
        )

    def bump(self, code: ExceptionCode, severity: Severity, n: int) -> None:
        """Register n additional occurrences of a code WITHOUT storing rows.

        Used when a stage samples a few detail rows but must still report the
        true total for the dashboard.
        """
        if n <= 0:
            return
        self._counts[code.value] = self._counts.get(code.value, 0) + n
        if severity != Severity.INFO:
            self._noninfo_total += n

    def extend(self, other: "ExceptionCollector") -> None:
        self._records.extend(other._records)
        for k, v in other._counts.items():
            self._counts[k] = self._counts.get(k, 0) + v
        self._noninfo_total += other._noninfo_total

    @property
    def records(self) -> list[ExceptionRecord]:
        return self._records

    def total_non_info(self) -> int:
        """True count of non-INFO exceptions (not capped)."""
        return self._noninfo_total

    def __len__(self) -> int:
        return sum(self._counts.values())    # true total across all codes

    def to_frame(self) -> pd.DataFrame:
        if not self._records:
            return pd.DataFrame(
                columns=[
                    "code", "severity", "stage", "record_id", "source_file",
                    "description", "expected_value", "actual_value", "difference",
                    "transaction_date",
                ]
            )
        return pd.DataFrame([r.to_dict() for r in self._records])

    def summary(self) -> pd.DataFrame:
        """Exception Type | Count | Percentage — using TRUE totals (uncapped)."""
        if not self._counts:
            return pd.DataFrame(columns=["exception_type", "count", "percentage"])
        total = sum(self._counts.values())
        rows = [{"exception_type": k, "count": v,
                 "percentage": round(v / total * 100, 2)}
                for k, v in sorted(self._counts.items(), key=lambda kv: -kv[1])]
        return pd.DataFrame(rows)
