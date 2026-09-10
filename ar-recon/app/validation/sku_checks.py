"""SKU checks: SKU Returned <= SKU Ordered (per packet + SKU).

Only runs when SKU/qty information is available on both the forward and reverse
sales frames. Records SKU_RETURNED_GT_ORDERED and missing/invalid-qty issues.
"""
from __future__ import annotations

import pandas as pd

from app.core.enums import ExceptionCode, Severity
from app.exceptions_framework import ExceptionCollector


def _agg_qty(df: pd.DataFrame) -> pd.DataFrame:
    d = df.copy()
    d["qty"] = pd.to_numeric(d.get("qty"), errors="coerce")
    return d.groupby(["packet_id", "sku_code"], dropna=False)["qty"].sum().reset_index()


def check_sku(
    forward: pd.DataFrame, reverse: pd.DataFrame, collector: ExceptionCollector,
    reverse_source: str = "reverse_sales",
) -> pd.DataFrame:
    have_cols = {"packet_id", "sku_code", "qty"}
    if not have_cols.issubset(forward.columns) or not have_cols.issubset(reverse.columns):
        return pd.DataFrame(columns=["packet_id", "sku_code", "ordered_qty", "returned_qty", "flag"])

    fo = _agg_qty(forward).rename(columns={"qty": "ordered_qty"})
    re = _agg_qty(reverse).rename(columns={"qty": "returned_qty"})
    merged = fo.merge(re, on=["packet_id", "sku_code"], how="outer")
    merged["ordered_qty"] = merged["ordered_qty"].fillna(0)
    merged["returned_qty"] = merged["returned_qty"].fillna(0)
    merged["flag"] = merged["returned_qty"] > merged["ordered_qty"]

    for _, r in merged[merged["flag"]].iterrows():
        collector.add(
            ExceptionCode.SKU_RETURNED_GT_ORDERED, r["packet_id"], reverse_source,
            f"SKU {r['sku_code']} returned {r['returned_qty']} > ordered {r['ordered_qty']}",
            severity=Severity.ERROR, stage="sku",
            expected_value=r["ordered_qty"], actual_value=r["returned_qty"],
            difference=float(r["returned_qty"] - r["ordered_qty"]),
        )
    missing_sku = merged["sku_code"].isna()
    for _, r in merged[missing_sku].iterrows():
        collector.add(
            ExceptionCode.MISSING_SKU, r["packet_id"], reverse_source,
            "Missing SKU code", severity=Severity.WARNING, stage="sku",
        )
    return merged
