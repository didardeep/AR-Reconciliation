"""RT vs RTO selection — a clearly-defined, configurable business rule.

The system must DETERMINE which reverse dataset applies to each forward packet
rather than blindly subtracting both RT and RTO.

Strategies (env ``RT_RTO_STRATEGY``):
  * ``indicator_then_presence`` (default): if an explicit indicator column
    (env ``RT_RTO_INDICATOR_COLUMN``, e.g. ``return_type``) is present in the
    reverse data, trust it; otherwise fall back to packet-id presence.
  * ``presence_only``: decide purely by whether the packet appears in RT / RTO.
  * ``indicator_only``: require the indicator column; if absent, mark ambiguous.

Rules enforced regardless of strategy:
  * a packet is assigned to at most one reverse type (never double-counted);
  * a packet present in BOTH RT and RTO -> ReverseType.AMBIGUOUS, flagged.
"""
from __future__ import annotations

from dataclasses import dataclass

import pandas as pd

from app.core.enums import ExceptionCode, ReverseType, Severity
from app.exceptions_framework import ExceptionCollector
from config.settings import get_settings


@dataclass
class ReverseDecision:
    """packet_id -> (reverse_type, reverse_value)."""
    frame: pd.DataFrame   # columns: packet_id, reverse_type, reverse_value


def _agg(df: pd.DataFrame) -> pd.DataFrame:
    if df is None or df.empty:
        return pd.DataFrame(columns=["packet_id", "value"])
    d = df.copy()
    d["shipment_value"] = pd.to_numeric(d["shipment_value"], errors="coerce").fillna(0.0)
    return (
        d.groupby("packet_id", dropna=False)["shipment_value"]
        .sum().reset_index().rename(columns={"shipment_value": "value"})
    )


def decide(
    forward_packets: pd.Series,
    rt: pd.DataFrame | None,
    rto: pd.DataFrame | None,
    collector: ExceptionCollector,
    *,
    strategy: str | None = None,
) -> ReverseDecision:
    settings = get_settings()
    strategy = strategy or settings.rt_rto_strategy
    indicator = settings.rt_rto_indicator_column

    rt_agg = _agg(rt).rename(columns={"value": "rt_value"})
    rto_agg = _agg(rto).rename(columns={"value": "rto_value"})

    fwd = pd.DataFrame({"packet_id": forward_packets.dropna().astype(str).unique()})
    m = fwd.merge(rt_agg.assign(packet_id=rt_agg.packet_id.astype(str)), on="packet_id", how="left")
    m = m.merge(rto_agg.assign(packet_id=rto_agg.packet_id.astype(str)), on="packet_id", how="left")
    m["in_rt"] = m["rt_value"].notna()
    m["in_rto"] = m["rto_value"].notna()

    # optional explicit indicator (packet_id -> {RT,RTO}) from the reverse files
    ind_map: dict[str, set[str]] = {}
    if strategy in ("indicator_then_presence", "indicator_only"):
        for df in (rt, rto):
            if df is not None and indicator in df.columns and "packet_id" in df.columns:
                for pid, val in df[["packet_id", indicator]].dropna().itertuples(index=False):
                    key = str(val).strip().upper()
                    if key in ("RT", "RTO"):
                        ind_map.setdefault(str(pid), set()).add(key)

    types: list[str] = []
    values: list[float] = []
    for row in m.itertuples(index=False):
        pid = row.packet_id
        rt_v = 0.0 if pd.isna(row.rt_value) else float(row.rt_value)
        rto_v = 0.0 if pd.isna(row.rto_value) else float(row.rto_value)
        ind = ind_map.get(pid, set())

        chosen = ReverseType.NONE
        value = 0.0

        # 1) explicit indicator wins when unambiguous
        if strategy in ("indicator_then_presence", "indicator_only") and ind:
            if ind == {"RT"}:
                chosen, value = ReverseType.RT, rt_v
            elif ind == {"RTO"}:
                chosen, value = ReverseType.RTO, rto_v
            else:  # indicator itself conflicting
                chosen = ReverseType.AMBIGUOUS
        elif strategy == "indicator_only":
            chosen = ReverseType.AMBIGUOUS if (row.in_rt or row.in_rto) else ReverseType.NONE

        # 2) presence-based fallback
        if chosen == ReverseType.NONE and strategy != "indicator_only":
            if row.in_rt and row.in_rto:
                chosen = ReverseType.AMBIGUOUS
            elif row.in_rt:
                chosen, value = ReverseType.RT, rt_v
            elif row.in_rto:
                chosen, value = ReverseType.RTO, rto_v

        if chosen == ReverseType.AMBIGUOUS:
            value = 0.0  # never double-count; hold for investigation
            collector.add(
                ExceptionCode.RT_RTO_AMBIGUITY, pid, "rt+rto",
                "Packet present in both RT and RTO (or conflicting indicator); "
                "reverse value withheld pending resolution",
                severity=Severity.ERROR,
                expected_value=f"RT={rt_v}, RTO={rto_v}",
            )
        types.append(chosen.value)
        values.append(round(value, 2))

    out = m[["packet_id"]].copy()
    out["reverse_type"] = types
    out["reverse_value"] = values
    return ReverseDecision(frame=out)
