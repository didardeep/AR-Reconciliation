"""Reconciliation run orchestrator.

Ties the pipeline together:
  ingest -> file/data-quality checks -> month checks -> sales recon ->
  settlement recon -> final recon -> summary cards -> (optional) persistence.

Runs fully on pandas so it is unit-testable without a database. Persistence to
PostgreSQL is a separate, optional step (:func:`persist`) using bulk inserts.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime

import pandas as pd

from app.core.enums import FileType, MatchStatus, ReverseType, Severity
from app.core.logging import get_logger
from app.exceptions_framework import ExceptionCollector
from app.ingestion.excel_reader import read_excel
from app.reconciliation.final import FinalReconciliation
from app.reconciliation.month_checks import check_month
from app.reconciliation.sales import SalesReconciliation
from app.reconciliation.settlement import SettlementReconciliation
from app.validation.data_quality import DataQualitySummary, profile
from config.settings import get_settings

log = get_logger(__name__)


@dataclass
class RunResult:
    sales: pd.DataFrame
    settlement: pd.DataFrame
    final: pd.DataFrame
    exceptions: ExceptionCollector
    quality: list[DataQualitySummary] = field(default_factory=list)
    summary_cards: dict = field(default_factory=dict)
    month_report: dict = field(default_factory=dict)


def _sum(df: pd.DataFrame, col: str) -> float:
    if df is None or df.empty or col not in df.columns:
        return 0.0
    return round(float(pd.to_numeric(df[col], errors="coerce").fillna(0).sum()), 2)


def run_reconciliation(
    *,
    sales_forward_path: str,
    settlement_forward_path: str,
    sales_rt_path: str | None = None,
    sales_rto_path: str | None = None,
    settlement_reverse_path: str | None = None,
    link_path: str | None = None,
    reconciliation_month: str,          # "YYYY-MM"
    tolerance: float | None = None,
) -> RunResult:
    """Run from Excel file PATHS (used by quick_check.py — no database)."""
    fwd = read_excel(sales_forward_path, FileType.SALES_FORWARD)
    rt = read_excel(sales_rt_path, FileType.SALES_RT) if sales_rt_path else None
    rto = read_excel(sales_rto_path, FileType.SALES_RTO) if sales_rto_path else None
    sf = read_excel(settlement_forward_path, FileType.SETTLEMENT_FORWARD)
    sr = read_excel(settlement_reverse_path, FileType.SETTLEMENT_REVERSE) if settlement_reverse_path else None
    link = read_excel(link_path, FileType.SALES_SETTLEMENT_LINK) if link_path else None
    return _run(fwd, rt, rto, sf, sr, link, reconciliation_month, tolerance)


def run_reconciliation_from_db(
    session,
    *,
    sales_forward_file_id: int,
    settlement_forward_file_id: int,
    sales_rt_file_id: int | None = None,
    sales_rto_file_id: int | None = None,
    settlement_reverse_file_id: int | None = None,
    link_file_id: int | None = None,
    reconciliation_month: str,
    tolerance: float | None = None,
) -> RunResult:
    """Run from rows already STORED in the database (the production path).

    No spreadsheet is read here — the Excel was decoded once at upload. This
    loads canonical DataFrames straight from Postgres (fast, indexed).
    """
    from app.db.load import load_frame

    def L(fid, ft):
        return load_frame(session, fid, ft) if fid else None

    fwd = load_frame(session, sales_forward_file_id, FileType.SALES_FORWARD)
    rt = L(sales_rt_file_id, FileType.SALES_RT)
    rto = L(sales_rto_file_id, FileType.SALES_RTO)
    sf = load_frame(session, settlement_forward_file_id, FileType.SETTLEMENT_FORWARD)
    sr = L(settlement_reverse_file_id, FileType.SETTLEMENT_REVERSE)
    link = L(link_file_id, FileType.SALES_SETTLEMENT_LINK)
    return _run(fwd, rt, rto, sf, sr, link, reconciliation_month, tolerance)


def _run(fwd, rt, rto, sf, sr, link, reconciliation_month, tolerance) -> RunResult:
    tolerance = get_settings().recon_tolerance if tolerance is None else tolerance
    year, month = (int(x) for x in reconciliation_month.split("-"))
    collector = ExceptionCollector(stage_default="run")
    # ---- data-quality checks ----
    quality: list[DataQualitySummary] = []
    for df, ft, name in [
        (fwd, FileType.SALES_FORWARD, "sales_forward"),
        (rt, FileType.SALES_RT, "sales_rt"),
        (rto, FileType.SALES_RTO, "sales_rto"),
        (sf, FileType.SETTLEMENT_FORWARD, "settlement_forward"),
        (sr, FileType.SETTLEMENT_REVERSE, "settlement_reverse"),
    ]:
        if df is not None:
            quality.append(profile(df, ft, name, collector))

    # ---- month checks (flag out-of-month; do not drop) ----
    month_report = {}
    for df, idc, name in [
        (fwd, "packet_id", "sales_forward"),
        (sf, "order_release_id", "settlement_forward"),
    ]:
        if df is not None and "transaction_date" in df.columns:
            r = check_month(df, idc, "transaction_date", year, month, name, collector)
            month_report[name] = r.__dict__

    # ---- stage 1: sales ----
    sales_svc = SalesReconciliation()
    sales_res = sales_svc.reconcile(fwd, rt, rto)
    collector.extend(sales_res.collector)

    # ---- stage 2: settlement ----
    settle_svc = SettlementReconciliation()
    settle_res = settle_svc.reconcile(sf, sr)
    collector.extend(settle_res.collector)

    # ---- final: sales vs settlement (mapping-aware) ----
    # Build packet_id <-> order_release_id link from settlement forward if it
    # carries both columns.  When ASSUME_PACKET_EQUALS_ORDER=true the identity
    # join (packet_id == order_release_id) is used instead — see final.py.
    settlement_packet_map = None
    if "packet_id" in sf.columns and "order_release_id" in sf.columns:
        settlement_packet_map = sf[["packet_id", "order_release_id"]].dropna().astype(str).drop_duplicates()
        log.info("Built packet_id <-> order_release_id link from settlement forward (%d mappings)",
                 len(settlement_packet_map))
    final_svc = FinalReconciliation()
    final_res = final_svc.reconcile(
        sales_res.frame, settle_res.frame,
        link=link, settlement_packet_map=settlement_packet_map, tolerance=tolerance,
    )
    collector.extend(final_res.collector)

    # ---- summary cards ----
    sales_df = sales_res.frame
    cards = {
        "total_forward_sales": _sum(sales_df, "forward_shipment_value"),
        "total_rt": round(float(sales_df.loc[sales_df.reverse_type == ReverseType.RT.value, "reverse_value"].sum()), 2),
        "total_rto": round(float(sales_df.loc[sales_df.reverse_type == ReverseType.RTO.value, "reverse_value"].sum()), 2),
        "net_sales": _sum(sales_df, "net_sales"),
        "forward_settlement": _sum(settle_res.frame, "forward_customer_paid_amt"),
        "reverse_settlement": _sum(settle_res.frame, "reverse_customer_paid_amt"),
        "net_settlement": _sum(settle_res.frame, "net_settlement"),
        "matched_records": int((final_res.frame["status"] == MatchStatus.MATCHED.value).sum()),
        "unmatched_records": int((final_res.frame["status"] == MatchStatus.EXCEPTION.value).sum()),
        "exception_records": collector.total_non_info(),
    }
    cards["difference"] = round(cards["net_sales"] - cards["net_settlement"], 2)

    log.info("Run complete: matched=%s unmatched=%s exceptions=%s",
             cards["matched_records"], cards["unmatched_records"], cards["exception_records"])

    return RunResult(
        sales=sales_res.frame, settlement=settle_res.frame, final=final_res.frame,
        exceptions=collector, quality=quality, summary_cards=cards, month_report=month_report,
    )
