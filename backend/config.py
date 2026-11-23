"""Central configuration for the ReconOne backend (Sales vs Settlement engine).
Everything here can be overridden with environment variables so nothing is hardcoded."""
import os


def _setup_libpq():
    """Help pure-Python psycopg (psycopg3) locate PostgreSQL's own signed libpq.dll on Windows.

    psycopg3 in pure-Python mode loads libpq via ctypes, which works where compiled driver
    DLLs are blocked by Windows Application Control / Smart App Control. We point the DLL
    search at a local PostgreSQL install's bin directory. No-op on non-Windows or if not
    found (the system libpq is used). Override the directory with RECONONE_LIBPQ_DIR.

    Imported first in the backend (see server.py / db.py) so it runs before any psycopg import.
    """
    import sys
    import glob
    if sys.platform != "win32":
        return
    cands = []
    env = os.getenv("RECONONE_LIBPQ_DIR")
    if env:
        cands.append(env)
    cands += sorted(glob.glob(r"C:\Program Files\PostgreSQL\*\bin"), reverse=True)
    cands += sorted(glob.glob(r"C:\Program Files (x86)\PostgreSQL\*\bin"), reverse=True)
    for d in cands:
        if d and os.path.exists(os.path.join(d, "libpq.dll")):
            try:
                os.add_dll_directory(d)
            except Exception:
                pass
            os.environ["PATH"] = d + os.pathsep + os.environ.get("PATH", "")
            break


_setup_libpq()

# PostgreSQL connection - the SAME database and tables as the AR Recon tool (ar_recon)
DATABASE_URL = os.getenv(
    "AR_RECON_DATABASE_URL",
    os.getenv("RECONONE_DATABASE_URL", "postgresql://postgres:postgres@localhost:5432/ar_recon"),
)

# Reconciliation defaults (all editable on the "Run reconciliation" screen)
DEFAULT_TOLERANCE = float(os.getenv("AR_RECON_TOLERANCE", "0.01"))     # |net_sales - net_settlement| <= tolerance => MATCHED
DEFAULT_RETURN_WINDOW_DAYS = int(os.getenv("AR_RECON_RETURN_WINDOW", "30"))
DEFAULT_COMMISSION_TOLERANCE = float(os.getenv("AR_RECON_COMM_TOL", "1.00"))  # INR variance allowed when re-performing tax / fees / settlement

# Fee checks on the settlement files (both % are of MRP). Commission: forward settlement only, per row.
# Logistics: per order, forward + reverse settlement added up. Blank commission % = commission check not run.
DEFAULT_COMMISSION_PCT = os.getenv("AR_RECON_COMMISSION_PCT", "")
DEFAULT_LOGISTICS_PCT = os.getenv("AR_RECON_LOGISTICS_PCT", "1")
LOGISTICS_GST_RATE = float(os.getenv("AR_RECON_LOGISTICS_GST_RATE", "18"))   # GST charged on the logistics fees
TCS_RATE = float(os.getenv("AR_RECON_TCS_RATE", "1"))          # TCS = taxable amount x 1%
TDS_RATE = float(os.getenv("AR_RECON_TDS_RATE", "0.2"))        # TDS = taxable amount x 0.2%
PG_FEE_RATE = float(os.getenv("AR_RECON_PG_FEE_RATE", "2"))    # payment gateway fee = customer paid x 2%

# Settlements / reverse settlements arrive T+N days after the sale / return. A sale or return in the last N days of
# the month without its counterpart is shown as "awaiting settlement (T+N)" - expected next month, not an error yet.
SETTLEMENT_LAG_DAYS = int(os.getenv("AR_RECON_SETTLEMENT_LAG_DAYS", "2"))

# Join keys / IDs are converted to upper case at upload so 'ab123' and 'AB123' match. Set to 0 if IDs can
# legitimately differ only by case.
UPPERCASE_IDS = os.getenv("AR_RECON_UPPERCASE_IDS", "1") == "1"

# Settlement files run into the next month by up to this many days (T+N); used by the date metadata flags.
# Rows of a month more than this share outside the month raise a warning.
OUTSIDE_MONTH_WARN_PCT = float(os.getenv("AR_RECON_OUTSIDE_MONTH_WARN_PCT", "5"))

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
