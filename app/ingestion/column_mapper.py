"""Configurable column mapping (forgiving).

Resolution happens in three tiers, safest first:

1. **Exact-normalized** — case, spaces, underscores and punctuation are stripped
   before comparing, so ``Order Release ID``, ``order_release_id`` and
   ``ORDER_RELEASE_ID`` all resolve with no configuration.
2. **Alias** — genuinely different wording listed in COLUMN_MAPPINGS below
   (e.g. ``seller order no`` -> order_release_id).
3. **Fuzzy suggestions** — for any required canonical still unresolved, the
   closest header candidates are *suggested* (never auto-applied to money
   columns), so a human confirms the mapping rather than the tool guessing
   which of several amount columns is authoritative.

Authoritative resolution (``map_columns``) uses tiers 1-2 only, so it is
deterministic. ``resolution_report`` adds tier-3 suggestions for missing ones.
"""
from __future__ import annotations

import difflib
import re
from functools import lru_cache

from app.core.enums import FileType


def _norm(s: str) -> str:
    return re.sub(r"[^a-z0-9]", "", str(s).lower())


def _tokens(s: str) -> set[str]:
    return set(re.split(r"[^a-z0-9]+", str(s).lower())) - {""}


# ── Column mappings (canonical_name -> list of accepted aliases) ─────
# Matching is case-insensitive and ignores spaces / underscores / punctuation,
# so "Order Release ID", "order_release_id" and "ORDER_RELEASE_ID" all resolve
# without needing to be listed explicitly. Add aliases here only when the
# source header is genuinely different wording.

COLUMN_MAPPINGS: dict[str, dict[str, list[str]]] = {
    "sales_forward": {
        "packet_id":        ["packet_id", "packet id", "packetid"],
        "shipment_value":   ["shipment_value", "shipment value", "customer_paid_amount", "taxable_amount"],
        "order_id":         ["order_id", "order id", "new_parent_id", "New_Parent_id"],
        "sku_code":         ["sku_code", "sku", "sku_id"],
        "qty":              ["qty", "quantity", "ordered_qty"],
        "transaction_date": ["order_created_date", "order_shipped_date", "order_date", "invoice_date"],
    },
    "sales_rt": {
        "packet_id":        ["packet_id", "packet id"],
        "shipment_value":   ["shipment_value", "shipment value", "customer_paid_amount"],
        "return_type":      ["return_type", "return type", "reverse_type"],
        "sku_code":         ["sku_code", "sku", "sku_id"],
        "qty":              ["qty", "quantity", "returned_qty"],
        "transaction_date": ["return_created_date", "return_date", "transaction_date", "restocked_date", "order_created_date"],
        "order_id":         ["order_id", "order id", "new_parent_id", "New_Parent_id"],
    },
    "sales_rto": {
        "packet_id":        ["packet_id", "packet id"],
        "shipment_value":   ["shipment_value", "shipment value", "customer_paid_amount"],
        "return_type":      ["return_type", "return type", "reverse_type"],
        "sku_code":         ["sku_code", "sku", "sku_id"],
        "qty":              ["qty", "quantity"],
        "transaction_date": ["rto_initiated_date", "rto_delivered_date", "transaction_date", "order_rto_date", "order_created_date"],
        "order_id":         ["order_id", "order id", "new_parent_id", "New_Parent_id"],
    },
    "settlement_forward": {
        "order_release_id":  ["order_release_id", "order release id", "seller_order_id"],
        "customer_paid_amt": ["customer_paid_amt", "customer paid amount", "customer_paid_amount"],
        "packet_id":         ["packet_id", "packet_ID", "packet id"],
        "transaction_date":  ["delivery_date", "packing_date", "settlement_date"],
        "sku_code":          ["sku_code", "sku", "sku_id"],
        "return_type":       ["return_type", "return type"],
        "return_id":         ["return_id", "return_ID"],
        "invoice_number":    ["invoice_number", "invoice_no"],
    },
    "settlement_reverse": {
        "order_release_id":  ["order_release_id", "order release id", "seller_order_id"],
        "customer_paid_amt": ["customer_paid_amt", "customer paid amount", "customer_paid_amount"],
        "packet_id":         ["packet_id", "packet_ID", "packet id"],
        "return_id":         ["return_id", "return_ID"],
        "transaction_date":  ["return_date", "delivery_date"],
        "sku_code":          ["sku_code", "sku", "sku_id"],
        "return_type":       ["return_type", "return type"],
        "invoice_number":    ["invoice_number", "invoice_no"],
    },
    "sales_settlement_link": {
        "packet_id":        ["packet_id", "packet id"],
        "order_release_id": ["order_release_id", "order release id"],
    },
}


@lru_cache
def canonical_lookup(file_type: FileType) -> dict[str, str]:
    """{normalized_alias: canonical_name}."""
    raw = COLUMN_MAPPINGS.get(file_type.value, {})
    lookup: dict[str, str] = {}
    for canonical, aliases in raw.items():
        lookup[_norm(canonical)] = canonical
        for alias in aliases or []:
            lookup[_norm(alias)] = canonical
    return lookup


@lru_cache
def _alias_order(file_type: FileType) -> tuple[str, ...]:
    raw = COLUMN_MAPPINGS.get(file_type.value, {})
    order: list[str] = []
    for canonical, aliases in raw.items():
        order.append(_norm(canonical))
        for a in aliases or []:
            order.append(_norm(a))
    return tuple(order)


def map_columns(headers: list[str], file_type: FileType) -> dict[str, str]:
    """Authoritative {raw_header: canonical} using exact-normalized + alias only.

    A canonical is assigned to at most one raw header (alias priority decides),
    so there are never duplicate canonical columns.
    """
    lookup = canonical_lookup(file_type)
    resolved: dict[str, str] = {}
    taken: set[str] = set()
    priority = {c: i for i, c in enumerate(_alias_order(file_type))}
    for h in sorted(headers, key=lambda x: priority.get(_norm(x), 10_000)):
        canonical = lookup.get(_norm(h))
        if canonical and canonical not in taken:
            resolved[h] = canonical
            taken.add(canonical)
    return resolved


def required_canonicals(file_type: FileType) -> list[str]:
    core = {
        FileType.SALES_FORWARD: ["packet_id", "shipment_value"],
        FileType.SALES_RT: ["packet_id", "shipment_value"],
        FileType.SALES_RTO: ["packet_id", "shipment_value"],
        FileType.SETTLEMENT_FORWARD: ["order_release_id", "customer_paid_amt"],
        FileType.SETTLEMENT_REVERSE: ["order_release_id", "customer_paid_amt"],
        FileType.SALES_SETTLEMENT_LINK: ["packet_id", "order_release_id"],
    }
    return core[file_type]


def _score(canonical: str, header: str) -> float:
    """0..1 similarity combining normalized ratio and token overlap."""
    a, b = _norm(canonical), _norm(header)
    ratio = difflib.SequenceMatcher(None, a, b).ratio()
    ta, tb = _tokens(canonical.replace("_", " ")), _tokens(header)
    jac = len(ta & tb) / len(ta | tb) if (ta | tb) else 0.0
    return round(0.6 * ratio + 0.4 * jac, 3)


def suggest_for_missing(
    headers: list[str], file_type: FileType, missing: list[str], top_n: int = 3
) -> dict[str, list[tuple[str, float]]]:
    """For each missing canonical, the closest unresolved headers (suggestions)."""
    resolved = set(map_columns(headers, file_type).keys())
    pool = [h for h in headers if h not in resolved and str(h).strip()]
    out: dict[str, list[tuple[str, float]]] = {}
    for canon in missing:
        scored = sorted(((h, _score(canon, h)) for h in pool), key=lambda x: -x[1])
        out[canon] = [(h, s) for h, s in scored[:top_n] if s > 0.25]
    return out


def resolution_report(headers: list[str], file_type: FileType) -> dict:
    """Full picture: what resolved, what's missing, and suggestions for missing."""
    resolved = map_columns(headers, file_type)
    missing = [c for c in required_canonicals(file_type) if c not in resolved.values()]
    return {
        "file_type": file_type.value,
        "resolved": resolved,
        "missing_required": missing,
        "suggestions": suggest_for_missing(headers, file_type, missing),
    }
