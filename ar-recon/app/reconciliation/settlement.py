"""Settlement reconciliation (Stage 2).

Net Settlement = Forward customer_paid_amt - Reverse customer_paid_amt,
grouped by order_release_id. Duplicate order_release_id values are aggregated
before reconciliation. Forward-only and reverse-only records are flagged.
"""
from __future__ import annotations

import pandas as pd

from app.core.enums import ExceptionCode, Severity
from app.reconciliation.base import ReconciliationService, ReconResult


class SettlementReconciliation(ReconciliationService):
    name = "settlement"

    def _group(self, df: pd.DataFrame, source: str) -> pd.DataFrame:
        d = df.copy()
        d["customer_paid_amt"] = self.to_amount(d["customer_paid_amt"])

        missing_id = d["order_release_id"].isna() | (
            d["order_release_id"].astype(str).str.strip() == ""
        )
        for ref in d.index[missing_id]:
            self.collector.add(
                ExceptionCode.MISSING_ORDER_RELEASE_ID, f"row:{ref}", source,
                "Row with no order_release_id", severity=Severity.ERROR,
            )
        bad_amt = d["customer_paid_amt"].isna()
        for ref in d.index[bad_amt & ~missing_id]:
            self.collector.add(
                ExceptionCode.MISSING_CUSTOMER_PAID_AMT, d.at[ref, "order_release_id"], source,
                "Missing/invalid customer_paid_amt (treated as 0)", severity=Severity.ERROR,
            )
        d = d[~missing_id]
        d["order_release_id"] = d["order_release_id"].astype(str).str.strip()
        d["customer_paid_amt"] = d["customer_paid_amt"].fillna(0.0).abs()  # reverse often stored -ve

        grouped = (
            d.groupby("order_release_id", dropna=False)["customer_paid_amt"]
            .agg(["sum", "count"]).reset_index()
            .rename(columns={"sum": "amount", "count": "line_count"})
        )
        for oid in grouped.loc[grouped["line_count"] > 1, "order_release_id"]:
            self.collector.add(
                ExceptionCode.DUPLICATE_ORDER_RELEASE_ID, oid, source,
                "Multiple settlement lines for order_release_id (aggregated)",
                severity=Severity.INFO,
            )
        return grouped

    def reconcile(
        self, forward: pd.DataFrame, reverse: pd.DataFrame | None = None,
    ) -> ReconResult:
        fwd = self._group(forward, "settlement_forward").rename(
            columns={"amount": "forward_customer_paid_amt", "line_count": "fwd_lines"}
        )
        if reverse is None or reverse.empty:
            rev = pd.DataFrame(columns=["order_release_id", "amount", "line_count"])
        else:
            rev = self._group(reverse, "settlement_reverse")
        rev = rev.rename(columns={"amount": "reverse_customer_paid_amt", "line_count": "rev_lines"})

        out = fwd.merge(rev, on="order_release_id", how="outer", indicator=True)
        out["forward_customer_paid_amt"] = out["forward_customer_paid_amt"].fillna(0.0)
        out["reverse_customer_paid_amt"] = out["reverse_customer_paid_amt"].fillna(0.0)

        for oid in out.loc[out["_merge"] == "right_only", "order_release_id"]:
            self.collector.add(
                ExceptionCode.SETTLEMENT_REVERSE_ONLY, oid, "settlement_reverse",
                "Reverse settlement with no forward settlement", severity=Severity.ERROR,
            )
        # forward-only is normal (no return) -> INFO, still surfaced
        for oid in out.loc[out["_merge"] == "left_only", "order_release_id"]:
            self.collector.add(
                ExceptionCode.SETTLEMENT_FORWARD_ONLY, oid, "settlement_forward",
                "Forward settlement with no reverse (no return)", severity=Severity.INFO,
            )

        out["net_settlement"] = (
            out["forward_customer_paid_amt"] - out["reverse_customer_paid_amt"]
        ).round(2)

        gt = out["reverse_customer_paid_amt"] > out["forward_customer_paid_amt"]
        for _, r in out[gt].iterrows():
            self.collector.add(
                ExceptionCode.SETTLEMENT_REVERSE_GT_FORWARD, r["order_release_id"], "settlement",
                "Reverse settlement exceeds forward settlement", severity=Severity.ERROR,
                expected_value=r["forward_customer_paid_amt"],
                actual_value=r["reverse_customer_paid_amt"],
                difference=float(r["reverse_customer_paid_amt"] - r["forward_customer_paid_amt"]),
            )

        out = out[[
            "order_release_id", "forward_customer_paid_amt",
            "reverse_customer_paid_amt", "net_settlement",
        ]]
        return ReconResult(name=self.name, frame=out, collector=self.collector)
