"""Reconciliation endpoints: run, fetch results, exceptions, summary, export."""
from __future__ import annotations

import io

import pandas as pd
from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import StreamingResponse
from sqlalchemy.orm import Session

from app.api.deps import get_session
from app.db.models import (
    ExceptionRow, FinalReconResult, ReconciliationRun,
    SalesReconResult, SettlementReconResult, UploadedFile,
)
from app.schemas.schemas import RunOut, RunRequest, SummaryCards
from app.services import run_service
from config.settings import get_settings

router = APIRouter(prefix="/api/reconciliation", tags=["reconciliation"])


def _path(session: Session, file_id: int | None) -> str | None:
    if file_id is None:
        return None
    rec = session.get(UploadedFile, file_id)
    if not rec:
        raise HTTPException(404, f"file {file_id} not found")
    return rec.stored_path


@router.post("/run", response_model=RunOut)
def run(req: RunRequest, session: Session = Depends(get_session)):
    tol = get_settings().recon_tolerance if req.tolerance is None else req.tolerance
    run_row = ReconciliationRun(
        reconciliation_month=req.reconciliation_month, tolerance=tol,
        rt_rto_strategy=get_settings().rt_rto_strategy, status="RUNNING",
    )
    session.add(run_row)
    session.flush()

    try:
        result = run_service.run_reconciliation_from_db(
            session,
            sales_forward_file_id=req.sales_forward_file_id,
            sales_rt_file_id=req.sales_rt_file_id,
            sales_rto_file_id=req.sales_rto_file_id,
            settlement_forward_file_id=req.settlement_forward_file_id,
            settlement_reverse_file_id=req.settlement_reverse_file_id,
            link_file_id=req.link_file_id,
            reconciliation_month=req.reconciliation_month, tolerance=tol,
        )
    except Exception as exc:  # noqa: BLE001
        run_row.status = "FAILED"
        run_row.notes = str(exc)
        raise HTTPException(500, f"reconciliation failed: {exc}") from exc

    _persist(session, run_row.id, result)
    run_row.status = "COMPLETED"
    run_row.total_matched = result.summary_cards["matched_records"]
    run_row.total_exceptions = result.summary_cards["exception_records"]
    run_row.summary_cards = result.summary_cards
    session.flush()
    return RunOut(id=run_row.id, reconciliation_month=run_row.reconciliation_month,
                  tolerance=run_row.tolerance, status=run_row.status,
                  total_matched=run_row.total_matched, total_exceptions=run_row.total_exceptions)


def _persist(session: Session, run_id: int, result: run_service.RunResult) -> None:
    session.bulk_save_objects([
        SalesReconResult(run_id=run_id, packet_id=str(r.packet_id),
                         forward_shipment_value=r.forward_shipment_value,
                         reverse_type=r.reverse_type, reverse_value=r.reverse_value,
                         net_sales=r.net_sales)
        for r in result.sales.itertuples(index=False)
    ])
    session.bulk_save_objects([
        SettlementReconResult(run_id=run_id, order_release_id=str(r.order_release_id),
                              forward_customer_paid_amt=r.forward_customer_paid_amt,
                              reverse_customer_paid_amt=r.reverse_customer_paid_amt,
                              net_settlement=r.net_settlement)
        for r in result.settlement.itertuples(index=False)
    ])
    session.bulk_save_objects([
        FinalReconResult(run_id=run_id,
                         packet_id=None if pd.isna(r.packet_id) else str(r.packet_id),
                         order_release_id=None if pd.isna(r.order_release_id) else str(r.order_release_id),
                         net_sales=None if pd.isna(r.net_sales) else float(r.net_sales),
                         net_settlement=None if pd.isna(r.net_settlement) else float(r.net_settlement),
                         difference=None if pd.isna(r.difference) else float(r.difference),
                         status=r.status, exception_type=r.exception_type)
        for r in result.final.itertuples(index=False)
    ])
    session.bulk_save_objects([
        ExceptionRow(run_id=run_id, **{k: (None if v == "" else v) for k, v in e.to_dict().items()})
        for e in result.exceptions.records
    ])


@router.get("/{run_id}", response_model=RunOut)
def get_run(run_id: int, session: Session = Depends(get_session)):
    r = session.get(ReconciliationRun, run_id)
    if not r:
        raise HTTPException(404, "run not found")
    return RunOut(id=r.id, reconciliation_month=r.reconciliation_month, tolerance=r.tolerance,
                  status=r.status, total_matched=r.total_matched, total_exceptions=r.total_exceptions)


@router.get("/{run_id}/sales")
def get_sales(run_id: int, session: Session = Depends(get_session),
              limit: int = Query(500, le=5000), offset: int = 0):
    q = session.query(SalesReconResult).filter_by(run_id=run_id).offset(offset).limit(limit)
    return [r.__dict__ | {"_sa_instance_state": None} and
            {k: v for k, v in r.__dict__.items() if k != "_sa_instance_state"} for r in q]


@router.get("/{run_id}/settlement")
def get_settlement(run_id: int, session: Session = Depends(get_session),
                   limit: int = Query(500, le=5000), offset: int = 0):
    q = session.query(SettlementReconResult).filter_by(run_id=run_id).offset(offset).limit(limit)
    return [{k: v for k, v in r.__dict__.items() if k != "_sa_instance_state"} for r in q]


@router.get("/{run_id}/results")
def get_results(run_id: int, session: Session = Depends(get_session),
                status: str | None = None, exception_type: str | None = None,
                q: str | None = None, limit: int = Query(500, le=5000), offset: int = 0):
    query = session.query(FinalReconResult).filter_by(run_id=run_id)
    if status:
        query = query.filter(FinalReconResult.status == status)
    if exception_type:
        query = query.filter(FinalReconResult.exception_type == exception_type)
    if q:
        like = f"%{q}%"
        query = query.filter(
            (FinalReconResult.packet_id.ilike(like)) |
            (FinalReconResult.order_release_id.ilike(like))
        )
    rows = query.offset(offset).limit(limit).all()
    return [{k: v for k, v in r.__dict__.items() if k != "_sa_instance_state"} for r in rows]


@router.get("/{run_id}/exceptions")
def get_exceptions(run_id: int, session: Session = Depends(get_session),
                   code: str | None = None, limit: int = Query(1000, le=10000), offset: int = 0):
    query = session.query(ExceptionRow).filter_by(run_id=run_id)
    if code:
        query = query.filter(ExceptionRow.code == code)
    rows = query.offset(offset).limit(limit).all()
    return [{k: v for k, v in r.__dict__.items() if k != "_sa_instance_state"} for r in rows]


@router.get("/{run_id}/summary")
def get_summary(run_id: int, session: Session = Depends(get_session)):
    run = session.get(ReconciliationRun, run_id)
    finals = session.query(FinalReconResult).filter_by(run_id=run_id).all()
    excs = session.query(ExceptionRow).filter_by(run_id=run_id).all()
    if not run and not finals and not excs:
        raise HTTPException(404, "run has no results")
    exc_df = pd.DataFrame([{"code": e.code} for e in excs])
    exc_summary = []
    if not exc_df.empty:
        vc = exc_df["code"].value_counts()
        total = int(vc.sum())
        exc_summary = [{"exception_type": k, "count": int(v), "percentage": round(v / total * 100, 2)}
                       for k, v in vc.items()]
    return {"cards": (run.summary_cards if run else None),
            "exception_summary": exc_summary,
            "matched": sum(1 for f in finals if f.status == "MATCHED"),
            "unmatched": sum(1 for f in finals if f.status == "EXCEPTION")}


@router.get("/{run_id}/export")
def export(run_id: int, kind: str = Query("final", pattern="^(final|sales|settlement|exceptions)$"),
           fmt: str = Query("csv", pattern="^(csv|excel)$"), session: Session = Depends(get_session)):
    model = {"final": FinalReconResult, "sales": SalesReconResult,
             "settlement": SettlementReconResult, "exceptions": ExceptionRow}[kind]
    rows = session.query(model).filter_by(run_id=run_id).all()
    df = pd.DataFrame([{k: v for k, v in r.__dict__.items() if k != "_sa_instance_state"} for r in rows])
    buf = io.BytesIO()
    if fmt == "excel":
        with pd.ExcelWriter(buf, engine="openpyxl") as w:
            df.to_excel(w, index=False, sheet_name=kind)
        media = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
        fn = f"{kind}_run{run_id}.xlsx"
    else:
        df.to_csv(buf, index=False)
        media, fn = "text/csv", f"{kind}_run{run_id}.csv"
    buf.seek(0)
    return StreamingResponse(buf, media_type=media,
                             headers={"Content-Disposition": f"attachment; filename={fn}"})
