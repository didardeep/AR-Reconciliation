"""PostgreSQL schema (SQLAlchemy 2.0 models).

Each uploaded file gets its OWN dedicated table (``upload_data_{file_id}``) with
real PostgreSQL columns matching the exact Excel headers. This means you can
browse the actual data in pgAdmin — every column, every row, exactly as uploaded.

The ``UploadedFile`` row stores the list of original column headers in
``column_headers`` (JSON) so the load path knows the table shape.
"""
from __future__ import annotations

from datetime import datetime

from sqlalchemy import (
    DateTime, Float, ForeignKey, Index, Integer, String, Text, func,
)
from sqlalchemy import JSON
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base

# JSONB on PostgreSQL, plain JSON elsewhere (e.g. SQLite) — same code, both work.
JSONType = JSON().with_variant(JSONB, "postgresql")


class UploadedFile(Base):
    __tablename__ = "uploaded_files"
    id: Mapped[int] = mapped_column(primary_key=True)
    filename: Mapped[str] = mapped_column(String(512))
    stored_path: Mapped[str] = mapped_column(String(1024))
    file_type: Mapped[str] = mapped_column(String(64), index=True)
    upload_date: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())
    reconciliation_month: Mapped[str | None] = mapped_column(String(7))  # YYYY-MM
    row_count: Mapped[int | None] = mapped_column(Integer)
    processing_status: Mapped[str] = mapped_column(String(32), default="UPLOADED")
    # Original Excel column headers — stored so the load path knows the dynamic table shape
    column_headers: Mapped[dict | None] = mapped_column(JSONType)


class ReconciliationRun(Base):
    __tablename__ = "reconciliation_runs"
    id: Mapped[int] = mapped_column(primary_key=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())
    reconciliation_month: Mapped[str] = mapped_column(String(7), index=True)
    tolerance: Mapped[float] = mapped_column(Float)
    rt_rto_strategy: Mapped[str] = mapped_column(String(64))
    status: Mapped[str] = mapped_column(String(32), default="PENDING", index=True)
    sales_matched: Mapped[int | None] = mapped_column(Integer)
    total_matched: Mapped[int | None] = mapped_column(Integer)
    total_exceptions: Mapped[int | None] = mapped_column(Integer)
    summary_cards: Mapped[dict | None] = mapped_column(JSONType)
    notes: Mapped[str | None] = mapped_column(Text)

    sales_results = relationship("SalesReconResult", back_populates="run")
    settlement_results = relationship("SettlementReconResult", back_populates="run")
    final_results = relationship("FinalReconResult", back_populates="run")
    sku_results = relationship("SkuReconResult", back_populates="run")
    exceptions = relationship("ExceptionRow", back_populates="run")


class SalesReconResult(Base):
    __tablename__ = "sales_reconciliation_results"
    id: Mapped[int] = mapped_column(primary_key=True)
    run_id: Mapped[int] = mapped_column(ForeignKey("reconciliation_runs.id"), index=True)
    packet_id: Mapped[str] = mapped_column(String(128), index=True)
    forward_shipment_value: Mapped[float] = mapped_column(Float)
    reverse_type: Mapped[str] = mapped_column(String(16))
    reverse_value: Mapped[float] = mapped_column(Float)
    net_sales: Mapped[float] = mapped_column(Float)
    run = relationship("ReconciliationRun", back_populates="sales_results")


class SettlementReconResult(Base):
    __tablename__ = "settlement_reconciliation_results"
    id: Mapped[int] = mapped_column(primary_key=True)
    run_id: Mapped[int] = mapped_column(ForeignKey("reconciliation_runs.id"), index=True)
    order_release_id: Mapped[str] = mapped_column(String(128), index=True)
    forward_customer_paid_amt: Mapped[float] = mapped_column(Float)
    reverse_customer_paid_amt: Mapped[float] = mapped_column(Float)
    net_settlement: Mapped[float] = mapped_column(Float)
    run = relationship("ReconciliationRun", back_populates="settlement_results")


class FinalReconResult(Base):
    __tablename__ = "final_reconciliation_results"
    id: Mapped[int] = mapped_column(primary_key=True)
    run_id: Mapped[int] = mapped_column(ForeignKey("reconciliation_runs.id"), index=True)
    packet_id: Mapped[str | None] = mapped_column(String(128), index=True)
    order_release_id: Mapped[str | None] = mapped_column(String(128), index=True)
    net_sales: Mapped[float | None] = mapped_column(Float)
    net_settlement: Mapped[float | None] = mapped_column(Float)
    difference: Mapped[float | None] = mapped_column(Float)
    status: Mapped[str] = mapped_column(String(16), index=True)
    exception_type: Mapped[str | None] = mapped_column(String(64))
    run = relationship("ReconciliationRun", back_populates="final_results")


class SkuReconResult(Base):
    __tablename__ = "sku_reconciliation_results"
    id: Mapped[int] = mapped_column(primary_key=True)
    run_id: Mapped[int] = mapped_column(ForeignKey("reconciliation_runs.id"), index=True)
    packet_id: Mapped[str] = mapped_column(String(128), index=True)
    sku_code: Mapped[str] = mapped_column(String(128), index=True)
    ordered_qty: Mapped[float] = mapped_column(Float)
    rt_qty: Mapped[float] = mapped_column(Float)
    rto_qty: Mapped[float] = mapped_column(Float)
    returned_qty: Mapped[float] = mapped_column(Float)
    flag: Mapped[str | None] = mapped_column(String(64))
    run = relationship("ReconciliationRun", back_populates="sku_results")


class ValidationResult(Base):
    __tablename__ = "validation_results"
    id: Mapped[int] = mapped_column(primary_key=True)
    run_id: Mapped[int] = mapped_column(ForeignKey("reconciliation_runs.id"), index=True)
    source_file: Mapped[str] = mapped_column(String(512))
    summary: Mapped[dict] = mapped_column(JSONType)


class ExceptionRow(Base):
    __tablename__ = "exceptions"
    id: Mapped[int] = mapped_column(primary_key=True)
    run_id: Mapped[int] = mapped_column(ForeignKey("reconciliation_runs.id"), index=True)
    code: Mapped[str] = mapped_column(String(64), index=True)
    severity: Mapped[str] = mapped_column(String(16), index=True)
    stage: Mapped[str] = mapped_column(String(32), index=True)
    record_id: Mapped[str] = mapped_column(String(128), index=True)
    source_file: Mapped[str] = mapped_column(String(512))
    description: Mapped[str] = mapped_column(Text)
    expected_value: Mapped[str | None] = mapped_column(String(256))
    actual_value: Mapped[str | None] = mapped_column(String(256))
    difference: Mapped[float | None] = mapped_column(Float)
    transaction_date: Mapped[str | None] = mapped_column(String(32))
    run = relationship("ReconciliationRun", back_populates="exceptions")


Index("ix_final_run_status", FinalReconResult.run_id, FinalReconResult.status)
Index("ix_exc_run_code", ExceptionRow.run_id, ExceptionRow.code)
Index("ix_sku_run_packet", SkuReconResult.run_id, SkuReconResult.packet_id)
