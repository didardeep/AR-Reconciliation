"""Sales reconciliation (Stage 1).

Net Sales = Forward shipment_value - applicable RT OR RTO shipment_value,
grouped by packet_id. Uses the RT/RTO selector to decide which reverse dataset
applies. Reverse-only packets (present in RT/RTO but not Forward) and other
anomalies are flagged, not dropped.
"""
from __future__ import annotations

import pandas as pd

from app.core.enums import ExceptionCode, ReverseType, Severity
from app.reconciliation.base import ReconciliationService, ReconResult
from app.reconciliation.rt_rto_selector import decide


class SalesReconciliation(ReconciliationService):
    name = "sales"

    def _group_forward(self, forward: pd.DataFrame) -> pd.DataFrame:
        df = forward.copy()
        df["shipment_value"] = self.to_amount(df["shipment_value"])

        # invalid / missing shipment values -> flagged, treated as 0 in the sum
        bad = df["shipment_value"].isna()
        for ref in df.index[bad]:
            self.collector.add(
                ExceptionCode.MISSING_SHIPMENT_VALUE, df.at[ref, "packet_id"], "sales_forward",
                "Missing/invalid forward shipment_value (treated as 0)",
                severity=Severity.ERROR,
            )
        df["shipment_value"] = df["shipment_value"].fillna(0.0)

        missing_pid = df["packet_id"].isna() | (df["packet_id"].astype(str).str.strip() == "")
        for ref in df.index[missing_pid]:
            self.collector.add(
                ExceptionCode.MISSING_PACKET_ID, f"row:{ref}", "sales_forward",
                "Forward row with no packet_id", severity=Severity.ERROR,
            )
        df = df[~missing_pid]
        df["packet_id"] = df["packet_id"].astype(str).str.strip()

        grouped = (
            df.groupby("packet_id", dropna=False)["shipment_value"]
            .agg(["sum", "count"]).reset_index()
            .rename(columns={"sum": "forward_shipment_value", "count": "forward_line_count"})
        )
        for pid in grouped.loc[grouped["forward_line_count"] > 1, "packet_id"]:
            self.collector.add(
                ExceptionCode.DUPLICATE_PACKET_ID, pid, "sales_forward",
                "Multiple forward lines for packet_id (aggregated)",
                severity=Severity.INFO,
            )
        return grouped

    def _flag_reverse_only(self, forward_pids: set[str], rt: pd.DataFrame | None,
                           rto: pd.DataFrame | None) -> None:
        def pids(df):
            if df is None or df.empty:
                return set()
            return set(df["packet_id"].dropna().astype(str).str.strip())
        for pid in pids(rt) - forward_pids:
            self.collector.add(
                ExceptionCode.RT_NOT_IN_FORWARD, pid, "sales_rt",
                "Packet in RT but not in Forward", severity=Severity.ERROR,
            )
        for pid in pids(rto) - forward_pids:
            self.collector.add(
                ExceptionCode.RTO_NOT_IN_FORWARD, pid, "sales_rto",
                "Packet in RTO but not in Forward", severity=Severity.ERROR,
            )

    def reconcile(
        self, forward: pd.DataFrame, rt: pd.DataFrame | None = None,
        rto: pd.DataFrame | None = None,
    ) -> ReconResult:
        grouped = self._group_forward(forward)
        forward_pids = set(grouped["packet_id"])
        self._flag_reverse_only(forward_pids, rt, rto)

        decision = decide(grouped["packet_id"], rt, rto, self.collector)
        out = grouped.merge(decision.frame, on="packet_id", how="left")
        out["reverse_type"] = out["reverse_type"].fillna(ReverseType.NONE.value)
        out["reverse_value"] = pd.to_numeric(out["reverse_value"], errors="coerce").fillna(0.0)
        out["net_sales"] = (out["forward_shipment_value"] - out["reverse_value"]).round(2)

        # reverse greater than forward -> flag (data / mapping issue)
        gt = out["reverse_value"] > out["forward_shipment_value"]
        for _, r in out[gt].iterrows():
            self.collector.add(
                ExceptionCode.REVERSE_GT_FORWARD, r["packet_id"], "sales",
                "Reverse value exceeds forward value",
                severity=Severity.ERROR,
                expected_value=r["forward_shipment_value"], actual_value=r["reverse_value"],
                difference=float(r["reverse_value"] - r["forward_shipment_value"]),
            )

        out = out[[
            "packet_id", "forward_shipment_value", "reverse_type",
            "reverse_value", "net_sales", "forward_line_count",
        ]]
        return ReconResult(name=self.name, frame=out, collector=self.collector)
