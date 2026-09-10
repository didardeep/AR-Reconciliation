"""Pydantic API schemas."""
from __future__ import annotations
from pydantic import BaseModel, Field


class FileOut(BaseModel):
    id: int
    filename: str
    file_type: str
    row_count: int | None = None
    processing_status: str


class RunRequest(BaseModel):
    reconciliation_month: str = Field(..., pattern=r"^\d{4}-\d{2}$", description="YYYY-MM")
    tolerance: float | None = Field(default=None, ge=0)
    sales_forward_file_id: int
    sales_rt_file_id: int | None = None
    sales_rto_file_id: int | None = None
    settlement_forward_file_id: int
    settlement_reverse_file_id: int | None = None
    link_file_id: int | None = None


class SummaryCards(BaseModel):
    total_forward_sales: float
    total_rt: float
    total_rto: float
    net_sales: float
    forward_settlement: float
    reverse_settlement: float
    net_settlement: float
    difference: float
    matched_records: int
    unmatched_records: int
    exception_records: int


class RunOut(BaseModel):
    id: int
    reconciliation_month: str
    tolerance: float
    status: str
    total_matched: int | None = None
    total_exceptions: int | None = None


class ExceptionSummaryItem(BaseModel):
    exception_type: str
    count: int
    percentage: float
