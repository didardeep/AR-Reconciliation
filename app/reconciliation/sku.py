"""SKU + quantity reconciliation (grain: packet_id + sku_code).

Value-only sales reconciliation (``sales.py``) can hide a real data error: a
return posted against the wrong SKU, or more units returned than were ever
shipped. This stage checks quantity per SKU line and decides which
(packet_id, sku_code) pairs must have their reverse value withheld from the
sales stage and which packets must be forced to EXCEPTION in the final stage,
regardless of whether the dollar totals tie out.

Three situations, on purpose treated differently:
  * a packet split across RT and RTO on DIFFERENT skus is legitimate — both
    net normally, flagged only as an INFO/WARNING for visibility.
  * the SAME (packet_id, sku_code) in both RT and RTO is a data error — that
    SKU's reverse value is withheld and the packet is forced to EXCEPTION.
  * returned qty > forward qty for a (packet_id, sku_code) is a physical
    impossibility — same treatment.

When sku_code/qty columns are unavailable this stage no-ops (empty frame,
one WARNING) and callers keep today's packet-level behaviour.
"""
from __future__ import annotations

from dataclasses import dataclass, field

import pandas as pd

from app.core.enums import ExceptionCode, Severity
from app.reconciliation.base import ReconciliationService, ReconResult

REQUIRED_COLS = {"packet_id", "sku_code", "qty"}


@dataclass
class SkuResult(ReconResult):
    # None means the SKU stage could not run at all (columns unavailable) --
    # callers must fall back to packet-level behaviour. An empty set means it
    # ran and found nothing to withhold.
    withheld: set[tuple[str, str]] | None = field(default_factory=set)
    packet_flags: dict[str, str] = field(default_factory=dict)


def _has_cols(df: pd.DataFrame | None) -> bool:
    return df is not None and not df.empty and REQUIRED_COLS.issubset(df.columns)


def _agg_qty(df: pd.DataFrame) -> pd.DataFrame:
    d = df.copy()
    d["packet_id"] = d["packet_id"].astype(str).str.strip()
    d["sku_code"] = d["sku_code"].astype(str).str.strip()
    d["qty"] = pd.to_numeric(d["qty"], errors="coerce").fillna(0)
    return d.groupby(["packet_id", "sku_code"], dropna=False)["qty"].sum().reset_index()


class SkuReconciliation(ReconciliationService):
    name = "sku"

    def reconcile(
        self,
        forward: pd.DataFrame,
        rt: pd.DataFrame | None = None,
        rto: pd.DataFrame | None = None,
    ) -> SkuResult:
        if not _has_cols(forward):
            self.collector.add(
                ExceptionCode.MISSING_SKU, "ALL", "sales_forward",
                "sku_code/qty not available on forward sales — SKU-level "
                "check skipped, packet-level RT/RTO exclusivity applies instead",
                severity=Severity.WARNING,
            )
            empty = pd.DataFrame(columns=[
                "packet_id", "sku_code", "ordered_qty", "rt_qty", "rto_qty",
                "returned_qty", "flag",
            ])
            return SkuResult(name=self.name, frame=empty, collector=self.collector, withheld=None)

        fo = _agg_qty(forward).rename(columns={"qty": "ordered_qty"})
        rt_a = _agg_qty(rt).rename(columns={"qty": "rt_qty"}) if _has_cols(rt) else \
            pd.DataFrame(columns=["packet_id", "sku_code", "rt_qty"])
        rto_a = _agg_qty(rto).rename(columns={"qty": "rto_qty"}) if _has_cols(rto) else \
            pd.DataFrame(columns=["packet_id", "sku_code", "rto_qty"])

        merged = fo.merge(rt_a, on=["packet_id", "sku_code"], how="outer")
        merged = merged.merge(rto_a, on=["packet_id", "sku_code"], how="outer")
        merged["ordered_qty"] = merged["ordered_qty"].fillna(0)
        merged["rt_qty"] = merged["rt_qty"].fillna(0)
        merged["rto_qty"] = merged["rto_qty"].fillna(0)
        merged["returned_qty"] = merged["rt_qty"] + merged["rto_qty"]

        in_both = (merged["rt_qty"] > 0) & (merged["rto_qty"] > 0)
        over_returned = merged["returned_qty"] > merged["ordered_qty"]
        merged["flag"] = None
        merged.loc[in_both, "flag"] = ExceptionCode.RT_RTO_AMBIGUITY.value
        merged.loc[over_returned & ~in_both, "flag"] = ExceptionCode.SKU_RETURNED_GT_ORDERED.value

        withheld: set[tuple[str, str]] = set()
        packet_flags: dict[str, str] = {}

        for _, r in merged[in_both].iterrows():
            withheld.add((r["packet_id"], r["sku_code"]))
            packet_flags[r["packet_id"]] = ExceptionCode.RT_RTO_AMBIGUITY.value
            self.collector.add(
                ExceptionCode.RT_RTO_AMBIGUITY, r["packet_id"], "sales_rt+sales_rto",
                f"SKU {r['sku_code']} present in both RT and RTO for this packet; "
                "reverse value withheld pending resolution",
                severity=Severity.ERROR,
                expected_value=f"rt_qty={r['rt_qty']}, rto_qty={r['rto_qty']}",
            )

        for _, r in merged[over_returned & ~in_both].iterrows():
            withheld.add((r["packet_id"], r["sku_code"]))
            packet_flags[r["packet_id"]] = ExceptionCode.SKU_RETURNED_GT_ORDERED.value
            self.collector.add(
                ExceptionCode.SKU_RETURNED_GT_ORDERED, r["packet_id"], "sales_rt+sales_rto",
                f"SKU {r['sku_code']} returned {r['returned_qty']} > ordered {r['ordered_qty']}",
                severity=Severity.ERROR,
                expected_value=r["ordered_qty"], actual_value=r["returned_qty"],
                difference=float(r["returned_qty"] - r["ordered_qty"]),
            )

        # split packets: different SKUs on the same packet, one via RT one via
        # RTO, both otherwise clean -> legitimate, but flagged for visibility.
        clean = merged[~in_both & ~over_returned]
        by_packet = clean.groupby("packet_id").agg(
            has_rt=("rt_qty", lambda s: (s > 0).any()),
            has_rto=("rto_qty", lambda s: (s > 0).any()),
        )
        split_packets = by_packet[by_packet.has_rt & by_packet.has_rto].index
        for pid in split_packets:
            self.collector.add(
                ExceptionCode.PACKET_SPLIT_RT_RTO, pid, "sales_rt+sales_rto",
                "Packet has different SKUs returned via RT and RTO — "
                "legitimate split, both amounts subtracted normally",
                severity=Severity.WARNING,
            )

        missing_sku = merged["sku_code"].isna() | (merged["sku_code"].astype(str).str.strip() == "")
        for _, r in merged[missing_sku].iterrows():
            self.collector.add(
                ExceptionCode.MISSING_SKU, r["packet_id"], "sales_forward",
                "Missing SKU code", severity=Severity.WARNING,
            )

        out = merged[[
            "packet_id", "sku_code", "ordered_qty", "rt_qty", "rto_qty",
            "returned_qty", "flag",
        ]]
        return SkuResult(
            name=self.name, frame=out, collector=self.collector,
            withheld=withheld, packet_flags=packet_flags,
        )
