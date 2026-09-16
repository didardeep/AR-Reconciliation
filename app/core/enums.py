"""Enumerations shared across the reconciliation engine."""
from __future__ import annotations
from enum import Enum


class FileType(str, Enum):
    SALES_FORWARD = "sales_forward"
    SALES_RT = "sales_rt"
    SALES_RTO = "sales_rto"
    SETTLEMENT_FORWARD = "settlement_forward"
    SETTLEMENT_REVERSE = "settlement_reverse"
    SALES_SETTLEMENT_LINK = "sales_settlement_link"


class ReverseType(str, Enum):
    RT = "RT"
    RTO = "RTO"
    NONE = "None"
    AMBIGUOUS = "Ambiguous"      # same packet_id+sku_code present in both RT and RTO
    MIXED = "Mixed"              # different SKUs on one packet split across RT and RTO


class MatchStatus(str, Enum):
    MATCHED = "MATCHED"
    EXCEPTION = "EXCEPTION"


class RunStatus(str, Enum):
    PENDING = "PENDING"
    RUNNING = "RUNNING"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"


class ExceptionCode(str, Enum):
    # --- Sales ---
    MISSING_PACKET_ID = "MISSING_PACKET_ID"
    DUPLICATE_PACKET_ID = "DUPLICATE_PACKET_ID"
    RT_NOT_IN_FORWARD = "RT_NOT_IN_FORWARD"
    RTO_NOT_IN_FORWARD = "RTO_NOT_IN_FORWARD"
    RT_RTO_AMBIGUITY = "RT_RTO_AMBIGUITY"
    PACKET_SPLIT_RT_RTO = "PACKET_SPLIT_RT_RTO"
    MISSING_SHIPMENT_VALUE = "MISSING_SHIPMENT_VALUE"
    INVALID_SHIPMENT_VALUE = "INVALID_SHIPMENT_VALUE"
    REVERSE_GT_FORWARD = "REVERSE_GT_FORWARD"
    # --- Settlement ---
    MISSING_ORDER_RELEASE_ID = "MISSING_ORDER_RELEASE_ID"
    MISSING_CUSTOMER_PAID_AMT = "MISSING_CUSTOMER_PAID_AMT"
    DUPLICATE_ORDER_RELEASE_ID = "DUPLICATE_ORDER_RELEASE_ID"
    SETTLEMENT_FORWARD_ONLY = "SETTLEMENT_FORWARD_ONLY"
    SETTLEMENT_REVERSE_ONLY = "SETTLEMENT_REVERSE_ONLY"
    INVALID_AMOUNT = "INVALID_AMOUNT"
    SETTLEMENT_REVERSE_GT_FORWARD = "SETTLEMENT_REVERSE_GT_FORWARD"
    # --- Final ---
    SALES_ONLY = "SALES_ONLY"
    SETTLEMENT_ONLY = "SETTLEMENT_ONLY"
    AMOUNT_MISMATCH = "AMOUNT_MISMATCH"
    MISSING_MAPPING = "MISSING_MAPPING"
    # --- Cross-cutting ---
    MONTH_MISMATCH = "MONTH_MISMATCH"
    MISSING_DATE = "MISSING_DATE"
    INVALID_DATE = "INVALID_DATE"
    SKU_RETURNED_GT_ORDERED = "SKU_RETURNED_GT_ORDERED"
    MISSING_SKU = "MISSING_SKU"


class Severity(str, Enum):
    INFO = "INFO"
    WARNING = "WARNING"
    ERROR = "ERROR"
