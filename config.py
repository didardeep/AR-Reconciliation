"""Central configuration for the AR reconciliation tool.
Everything here can be overridden with environment variables so nothing is hardcoded."""
import os

# PostgreSQL connection (create the database first in pgAdmin: CREATE DATABASE ar_recon;)
DATABASE_URL = os.getenv(
    "AR_RECON_DATABASE_URL",
    "postgresql://postgres:postgres@localhost:5432/ar_recon",
)

# Reconciliation defaults (all editable on the "Run reconciliation" screen)
DEFAULT_TOLERANCE = float(os.getenv("AR_RECON_TOLERANCE", "0.01"))     # |net_sales - net_settlement| <= tolerance => MATCHED
DEFAULT_RETURN_WINDOW_DAYS = int(os.getenv("AR_RECON_RETURN_WINDOW", "30"))
DEFAULT_COMMISSION_TOLERANCE = float(os.getenv("AR_RECON_COMM_TOL", "1.00"))  # INR variance allowed when re-performing commission / tax

# Screen limits: anything beyond MAX_SCREEN_ROWS is only available via the Excel download
PAGE_SIZE = 100
MAX_SCREEN_ROWS = 5000

# Which date column is compared against the selected reconciliation month, per dataset.
# QUESTION FOR CLIENT/SPOC: confirm these are the right "period" columns.
MONTH_CHECK_COLUMNS = {
    "sales_forward":      "order_packed_date",
    "sales_rt":           "fr_refunded_date",
    "sales_rto":          "order_rto_date",
    "settlement_forward": "packing_date",
    "settlement_reverse": "return_date",
}

# Date columns that must be parseable if populated (hygiene check)
DATE_COLUMNS = {
    "sales_forward":      ["order_created_date", "order_packed_date", "order_shipped_date", "restocked_date", "order_rto_date"],
    "sales_rt":           ["order_created_date", "order_packed_date", "order_shipped_date", "order_delivered_date", "fr_refunded_date", "fr_restocked_date"],
    "sales_rto":          ["order_created_date", "order_packed_date", "order_shipped_date", "restocked_date", "order_rto_date", "order_cancel_date", "order_delivered_date"],
    "settlement_forward": ["packing_date", "delivery_date", "return_date"],
    "settlement_reverse": ["packing_date", "delivery_date", "return_date"],
}

# Amount columns that must be numeric if populated (hygiene check)
AMOUNT_COLUMNS = {
    "sales_forward":      ["shipment_value", "tax_amount", "MRP", "Net_Amount", "base_amount"],
    "sales_rt":           ["shipment_value", "MRP", "Net_Amount", "base_amount"],
    "sales_rto":          ["shipment_value", "tax_amount", "MRP", "Net_Amount", "base_amount"],
    "settlement_forward": ["customer_paid_amount", "total_commission", "total_actual_settlement", "total_expected_settlement", "taxable_amount"],
    "settlement_reverse": ["customer_paid_amount", "total_commission", "total_actual_settlement", "taxable_amount"],
}

# Return-window check: return event date minus forward shipped date must be <= return window
RETURN_WINDOW_DATES = {
    "sales_rt":  ("order_shipped_date", "fr_refunded_date"),
    "sales_rto": ("order_shipped_date", "order_rto_date"),
}
