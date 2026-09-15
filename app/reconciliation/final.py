"""Final AR reconciliation: Net Sales vs Net Settlement.

Difference = Net Sales - Net Settlement.
MATCHED when abs(Difference) <= configurable tolerance, else EXCEPTION.

packet_id and order_release_id are NOT assumed to be the same identifier. A
mapping is required to join the two sides:
  * if a link table (packet_id <-> order_release_id) is supplied, it is used;
  * else if the settlement frames carried a packet_id column, that is used;
  * else the join cannot be made and every record is flagged MISSING_MAPPING —
    the app never invents a relationship between the two IDs.
"""
from __future__ import annotations

import pandas as pd

from app.core.enums import ExceptionCode, MatchStatus, Severity
from app.reconciliation.base import ReconciliationService, ReconResult
from config.settings import get_settings


def _norm_id(series: pd.Series) -> pd.Series:
    """Normalize an ID column for reliable joining across files.

    Fixes the classic large-file mismatch where the same id is read as a number
    in one file ("12345.0") and text in another ("12345"): strips whitespace and
    a trailing ".0" so keys compare equal regardless of how each file typed them.
    """
    s = series.astype("string").str.strip()
    s = s.str.replace(r"\.0+$", "", regex=True)   # 12345.0 -> 12345
    return s


class FinalReconciliation(ReconciliationService):
    name = "final"

    def _build_link(
        self, sales: pd.DataFrame, settlement: pd.DataFrame,
        link: pd.DataFrame | None, settlement_packet_map: pd.DataFrame | None,
    ) -> pd.DataFrame | None:
        """Return a packet_id <-> order_release_id frame, or None if impossible."""
        if link is not None and not link.empty:
            out = link[["packet_id", "order_release_id"]].dropna()
            out["packet_id"] = _norm_id(out["packet_id"])
            out["order_release_id"] = _norm_id(out["order_release_id"])
            return out
        if settlement_packet_map is not None and not settlement_packet_map.empty:
            out = settlement_packet_map[["packet_id", "order_release_id"]].dropna().drop_duplicates()
            out["packet_id"] = _norm_id(out["packet_id"])
            out["order_release_id"] = _norm_id(out["order_release_id"])
            return out
        # Opt-in: user asserts the two IDs are the same value -> identity join.
        if get_settings().assume_packet_equals_order:
            ids = pd.Index(_norm_id(sales["packet_id"]).dropna()).union(
                _norm_id(settlement["order_release_id"]).dropna()
            )
            return pd.DataFrame({"packet_id": ids, "order_release_id": ids})
        return None

    def reconcile(
        self,
        sales_result: pd.DataFrame,
        settlement_result: pd.DataFrame,
        link: pd.DataFrame | None = None,
        settlement_packet_map: pd.DataFrame | None = None,
        tolerance: float | None = None,
    ) -> ReconResult:
        tolerance = get_settings().recon_tolerance if tolerance is None else tolerance
        link_tbl = self._build_link(sales_result, settlement_result, link, settlement_packet_map)

        sales = sales_result.copy()
        settle = settlement_result.copy()
        sales["packet_id"] = _norm_id(sales["packet_id"])
        settle["order_release_id"] = _norm_id(settle["order_release_id"])

        if link_tbl is None:
            # No mapping available -> cannot join; surface every record.
            out = sales[["packet_id", "net_sales"]].copy()
            out["order_release_id"] = pd.NA
            out["net_settlement"] = pd.NA
            out["difference"] = pd.NA
            out["status"] = MatchStatus.EXCEPTION.value
            out["exception_type"] = ExceptionCode.MISSING_MAPPING.value
            self.collector.add(
                ExceptionCode.MISSING_MAPPING, "ALL", "final",
                "No packet_id <-> order_release_id mapping supplied; Sales and "
                "Settlement cannot be joined. Provide a link table or a settlement "
                "packet_id column.", severity=Severity.ERROR,
            )
            return ReconResult(name=self.name, frame=out, collector=self.collector)

        s = sales.merge(link_tbl, on="packet_id", how="left")
        merged = s.merge(settle, on="order_release_id", how="outer", indicator=True)

        merged["net_sales"] = pd.to_numeric(merged["net_sales"], errors="coerce")
        merged["net_settlement"] = pd.to_numeric(merged["net_settlement"], errors="coerce")
        merged["difference"] = (merged["net_sales"].fillna(0) - merged["net_settlement"].fillna(0)).round(2)

        # --- vectorized classification (no per-row Python loop) ---
        import numpy as np
        sales_only = (merged["_merge"] == "left_only") | merged["order_release_id"].isna()
        settle_only = (merged["_merge"] == "right_only") | merged["packet_id"].isna()
        within = merged["difference"].abs() <= tolerance
        merged["exception_type"] = np.select(
            [sales_only, settle_only, within],
            [ExceptionCode.SALES_ONLY.value, ExceptionCode.SETTLEMENT_ONLY.value, None],
            default=ExceptionCode.AMOUNT_MISMATCH.value,
        )
        merged["status"] = np.where(
            merged["exception_type"].isna(), MatchStatus.MATCHED.value, MatchStatus.EXCEPTION.value
        )

        # --- exception feed: vectorized, and capped per type so we never build
        #     hundreds of thousands of objects. Full counts still come from the
        #     result frame; the collector holds a reviewable sample + a summary. ---
        self._log_exceptions_bulk(merged, tolerance)

        out = merged[[
            "packet_id", "order_release_id", "net_sales", "net_settlement",
            "difference", "status", "exception_type",
        ]]
        return ReconResult(name=self.name, frame=out, collector=self.collector)

    def _log_exceptions_bulk(self, merged: pd.DataFrame, tolerance: float,
                             cap_per_type: int = 5000) -> None:
        """Record exceptions without a per-row Python loop.

        For each exception type we keep at most ``cap_per_type`` sample rows plus
        one summary line with the true total, so a run with 300k mismatches does
        not build 300k objects or insert 300k DB rows. The result frame retains
        every row, so dashboard totals remain exact.
        """
        code_map = {
            ExceptionCode.SALES_ONLY.value: (ExceptionCode.SALES_ONLY, Severity.WARNING),
            ExceptionCode.SETTLEMENT_ONLY.value: (ExceptionCode.SETTLEMENT_ONLY, Severity.WARNING),
            ExceptionCode.AMOUNT_MISMATCH.value: (ExceptionCode.AMOUNT_MISMATCH, Severity.ERROR),
        }
        exc = merged[merged["status"] == MatchStatus.EXCEPTION.value]
        for etype, grp in exc.groupby("exception_type"):
            code, sev = code_map.get(etype, (ExceptionCode.AMOUNT_MISMATCH, Severity.ERROR))
            total = len(grp)
            sample = grp.head(cap_per_type)
            rid = sample["packet_id"].where(sample["packet_id"].notna(), sample["order_release_id"])
            for record_id, ns, nse, diff in zip(
                rid.astype("string"),
                sample["net_sales"], sample["net_settlement"], sample["difference"],
            ):
                self.collector.add(
                    code, record_id, "final",
                    f"{etype} (diff={None if pd.isna(diff) else round(float(diff),2)}, tol={tolerance})",
                    severity=sev,
                    expected_value=None if pd.isna(ns) else float(ns),
                    actual_value=None if pd.isna(nse) else float(nse),
                    difference=None if pd.isna(diff) else float(diff),
                )
            # register the true remainder so dashboard totals are exact
            self.collector.bump(code, sev, total - len(sample))
