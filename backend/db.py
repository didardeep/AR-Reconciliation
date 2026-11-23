"""PostgreSQL access layer: connections, schema creation, safe-cast helper functions, bulk COPY load."""
import csv
import io
from contextlib import contextmanager

import config  # noqa: F401 (imported first: sets up libpq on Windows before psycopg loads)
import psycopg
from psycopg import sql
from psycopg.rows import dict_row

from schema import DATASETS


def get_conn():
    return psycopg.connect(config.DATABASE_URL)


@contextmanager
def cursor(commit=True):
    conn = get_conn()
    try:
        cur = conn.cursor(row_factory=dict_row)
        yield cur
        if commit:
            conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def q(name):
    """Quoted identifier - keeps the exact (case-sensitive) column names from the files."""
    return sql.Identifier(name)


HELPER_FUNCTIONS = """
-- Safe numeric cast: returns NULL instead of failing (mirrors CAST(... AS NUMERIC) in the source SQL)
-- also reads "1,234.50", "₹ 1,200", "Rs. 1200", "INR 1200", "(150)" = -150 and "150-" = -150
CREATE OR REPLACE FUNCTION try_numeric(t text) RETURNS numeric AS $$
DECLARE s text; neg boolean := false;
BEGIN
    IF t IS NULL OR btrim(t) = '' THEN RETURN NULL; END IF;
    s := btrim(t);
    BEGIN
        RETURN replace(s, ',', '')::numeric;                        -- fast path: plain number
    EXCEPTION WHEN others THEN NULL;
    END;
    s := regexp_replace(s, '(₹|Rs\\.?|INR)', '', 'gi');
    s := replace(replace(s, ',', ''), ' ', '');
    IF s ~ '^\\(.*\\)$' THEN neg := true; s := substr(s, 2, length(s) - 2); END IF;
    IF s ~ '-$' THEN neg := true; s := left(s, -1); END IF;
    RETURN CASE WHEN neg THEN -(s::numeric) ELSE s::numeric END;
EXCEPTION WHEN others THEN RETURN NULL;
END $$ LANGUAGE plpgsql IMMUTABLE;

-- Safe date cast supporting ISO (2024-02-05, 2024-02-05 12:42:00, 20240205), DD-MM-YYYY, DD/MM/YYYY
CREATE OR REPLACE FUNCTION try_date(t text) RETURNS date AS $$
DECLARE s text := btrim(t);
BEGIN
    IF s IS NULL OR s = '' THEN RETURN NULL; END IF;
    BEGIN RETURN s::date; EXCEPTION WHEN others THEN NULL; END;
    BEGIN RETURN to_date(s, 'DD-MM-YYYY'); EXCEPTION WHEN others THEN NULL; END;
    BEGIN RETURN to_date(s, 'DD/MM/YYYY'); EXCEPTION WHEN others THEN NULL; END;
    BEGIN RETURN to_date(substring(s, 1, 10), 'DD-MM-YYYY'); EXCEPTION WHEN others THEN NULL; END;
    BEGIN RETURN to_date(substring(s, 1, 10), 'MM/DD/YYYY'); EXCEPTION WHEN others THEN NULL; END;
    RETURN NULL;
END $$ LANGUAGE plpgsql IMMUTABLE;
"""

RUN_TABLES = """
CREATE TABLE IF NOT EXISTS recon_runs (
    run_id            SERIAL PRIMARY KEY,
    created_at        TIMESTAMP DEFAULT now(),
    recon_month       DATE,
    tolerance         NUMERIC,
    return_window     INTEGER,
    commission_tol    NUMERIC,
    platform_filter   TEXT,
    status            TEXT DEFAULT 'RUNNING',
    total_sales       INTEGER, total_settlement INTEGER,
    matched           INTEGER, unmatched INTEGER,
    sales_only        INTEGER, settlement_only INTEGER,
    exception_count   INTEGER, hygiene_count INTEGER,
    message           TEXT
);
-- column mapping used by the run (JSON: {dataset: {field: column}}), kept for the audit trail / Excel output
ALTER TABLE recon_runs ADD COLUMN IF NOT EXISTS column_mapping TEXT;

CREATE TABLE IF NOT EXISTS recon_results (
    id                  BIGSERIAL PRIMARY KEY,
    run_id              INTEGER REFERENCES recon_runs(run_id) ON DELETE CASCADE,
    packet_id           TEXT,
    order_release_id    TEXT,
    forward_sales       NUMERIC,
    return_amount       NUMERIC,
    return_source       TEXT,
    net_sales           NUMERIC,
    forward_settlement  NUMERIC,
    reverse_settlement  NUMERIC,
    net_settlement      NUMERIC,
    difference          NUMERIC,
    status              TEXT
);
CREATE INDEX IF NOT EXISTS ix_results_run_status ON recon_results(run_id, status);

CREATE TABLE IF NOT EXISTS recon_exceptions (
    id               BIGSERIAL PRIMARY KEY,
    run_id           INTEGER REFERENCES recon_runs(run_id) ON DELETE CASCADE,
    category         TEXT,          -- HYGIENE or RECONCILIATION
    exception_type   TEXT,
    dataset          TEXT,          -- source table the row comes from (if any)
    row_no           INTEGER,       -- row number in the uploaded file (if any)
    packet_id        TEXT,
    order_release_id TEXT,
    amount_1         NUMERIC,
    amount_2         NUMERIC,
    difference       NUMERIC,
    description      TEXT
);
CREATE INDEX IF NOT EXISTS ix_exceptions_run_type ON recon_exceptions(run_id, exception_type);

CREATE TABLE IF NOT EXISTS upload_log (
    id           SERIAL PRIMARY KEY,
    dataset      TEXT,
    file_name    TEXT,
    sheet        TEXT,
    rows_loaded  INTEGER,
    mode         TEXT,
    added_columns TEXT,
    missing_columns TEXT,
    loaded_at    TIMESTAMP DEFAULT now()
);
-- headers actually present in the uploaded file (JSON: {table column: header as written in the file});
-- this is what the column-mapping dropdowns on the Run screen are filled from
ALTER TABLE upload_log ADD COLUMN IF NOT EXISTS file_columns TEXT;

-- last column mapping chosen on the Run screen, per dataset and logical field (re-used as the default next time)
CREATE TABLE IF NOT EXISTS column_mappings (
    dataset      TEXT NOT NULL,
    role         TEXT NOT NULL,
    column_name  TEXT,
    updated_at   TIMESTAMP DEFAULT now(),
    PRIMARY KEY (dataset, role)
);

-- one row per check per dataset per run, whether it passed, failed or could not run
CREATE TABLE IF NOT EXISTS recon_check_log (
    id               BIGSERIAL PRIMARY KEY,
    run_id           INTEGER REFERENCES recon_runs(run_id) ON DELETE CASCADE,
    seq              INTEGER,
    category         TEXT,          -- HYGIENE or RECONCILIATION
    check_code       TEXT,          -- same code as recon_exceptions.exception_type
    check_name       TEXT,
    description      TEXT,
    dataset          TEXT,
    columns_checked  TEXT,
    records_checked  INTEGER,
    records_failed   INTEGER,
    status           TEXT,          -- PASS / FAIL / NOT RUN
    note             TEXT
);
CREATE INDEX IF NOT EXISTS ix_check_log_run ON recon_check_log(run_id, seq);

-- data month of each upload, and what the value cleaning changed (JSON {fix: number of values})
ALTER TABLE upload_log ADD COLUMN IF NOT EXISTS period TEXT;
ALTER TABLE upload_log ADD COLUMN IF NOT EXISTS cleaning TEXT;

-- date / amount profile of each dataset per data month (refreshed after every upload)
CREATE TABLE IF NOT EXISTS data_profile (
    dataset        TEXT NOT NULL,
    period         TEXT NOT NULL,
    date_column    TEXT,
    amount_column  TEXT,
    rows           INTEGER,
    blank_dates    INTEGER,
    bad_dates      INTEGER,
    min_date       DATE,
    max_date       DATE,
    before_month   INTEGER,
    in_month       INTEGER,
    after_month    INTEGER,
    total_amount   NUMERIC,
    per_day        TEXT,           -- JSON {YYYY-MM-DD: rows}
    computed_at    TIMESTAMP DEFAULT now(),
    PRIMARY KEY (dataset, period)
);

-- carry-forward: every key that was not Matched in a run stays here (is_open) until a later run matches it
CREATE TABLE IF NOT EXISTS open_items (
    item_key          TEXT NOT NULL,              -- packet_id = order_release_id
    platform          TEXT NOT NULL DEFAULT '',   -- platform filter of the runs it belongs to ('' = all)
    first_month       DATE,                       -- month in which it was first left open
    first_run_id      INTEGER,
    last_month        DATE,                       -- month of the latest run that looked at it
    last_run_id       INTEGER,
    status            TEXT,                       -- latest status (SALES_HIGHER_THAN_SETTLEMENT / SALES_NOT_IN_SETTLEMENT / ...)
    net_sales         NUMERIC,
    net_settlement    NUMERIC,
    difference        NUMERIC,
    sale_date         DATE,                       -- period date of the sale (for ageing / T+N)
    awaiting_settlement BOOLEAN DEFAULT false,    -- sale in the last N days of the month: settlement expected next month
    is_open           BOOLEAN DEFAULT true,
    closed_month      DATE,
    closed_run_id     INTEGER,
    PRIMARY KEY (item_key, platform)
);
CREATE INDEX IF NOT EXISTS ix_open_items_open ON open_items(platform, is_open);

ALTER TABLE recon_runs ADD COLUMN IF NOT EXISTS recon_month_to DATE;      -- last month of the run (= recon_month for a one-month run)
ALTER TABLE recon_runs ADD COLUMN IF NOT EXISTS carried_in INTEGER;        -- open items brought in from earlier months
ALTER TABLE recon_runs ADD COLUMN IF NOT EXISTS closed_prior INTEGER;      -- ... of which matched (closed) in this run
ALTER TABLE recon_runs ADD COLUMN IF NOT EXISTS new_open INTEGER;          -- items of this month left open
ALTER TABLE recon_runs ADD COLUMN IF NOT EXISTS open_total INTEGER;        -- all open items after the run
ALTER TABLE recon_runs ADD COLUMN IF NOT EXISTS awaiting_settlement INTEGER;
ALTER TABLE recon_results ADD COLUMN IF NOT EXISTS carried BOOLEAN DEFAULT false;   -- key came from an earlier month
ALTER TABLE recon_results ADD COLUMN IF NOT EXISTS first_month DATE;                -- month the key was first open
-- exceptions shown in the result itself (the separate Exceptions page was removed)
ALTER TABLE recon_results ADD COLUMN IF NOT EXISTS exceptions TEXT[];                -- reconciliation exception codes of the key
ALTER TABLE recon_results ADD COLUMN IF NOT EXISTS hygiene_flags TEXT[];             -- hygiene check codes the key's rows failed
-- "Unmatched" split by direction (unmatched = sales_higher + settlement_higher)
ALTER TABLE recon_runs ADD COLUMN IF NOT EXISTS sales_higher INTEGER;
ALTER TABLE recon_runs ADD COLUMN IF NOT EXISTS return_not_adjusted INTEGER;   -- return processed but settlement not adjusted
ALTER TABLE recon_runs ADD COLUMN IF NOT EXISTS settlement_higher INTEGER;
ALTER TABLE recon_runs ADD COLUMN IF NOT EXISTS reverse_only INTEGER;
-- fee checks: allowed commission % / logistics % of MRP (NULL = check not run)
ALTER TABLE recon_runs ADD COLUMN IF NOT EXISTS commission_pct NUMERIC;
ALTER TABLE recon_runs ADD COLUMN IF NOT EXISTS logistics_pct NUMERIC;
ALTER TABLE recon_runs ADD COLUMN IF NOT EXISTS logistics_gst_rate NUMERIC;
ALTER TABLE recon_runs ADD COLUMN IF NOT EXISTS tcs_rate NUMERIC;
ALTER TABLE recon_runs ADD COLUMN IF NOT EXISTS tds_rate NUMERIC;
ALTER TABLE recon_runs ADD COLUMN IF NOT EXISTS pg_rate NUMERIC;

-- payments of the settlement file added up per bank UTR: one expected bank credit each (bank match)
CREATE TABLE IF NOT EXISTS recon_utr (
    id               BIGSERIAL PRIMARY KEY,
    run_id           INTEGER REFERENCES recon_runs(run_id) ON DELETE CASCADE,
    payment_type     TEXT,          -- Prepaid / Postpaid
    utr              TEXT,
    settlement_date  DATE,
    orders           INTEGER,
    rows             INTEGER,
    amount           NUMERIC
);
CREATE INDEX IF NOT EXISTS ix_recon_utr_run ON recon_utr(run_id);
"""


def ensure_schema():
    """Create the five dataset tables (exact column names), helper functions and run tables."""
    with cursor() as cur:
        for name, spec in DATASETS.items():
            cols = [sql.SQL("{} INTEGER").format(q("_row_no")), sql.SQL("{} TEXT").format(q("_source_file"))]
            cols += [sql.SQL("{} TEXT").format(q(c)) for c in spec["columns"]]
            cur.execute(sql.SQL("CREATE TABLE IF NOT EXISTS {} ({})").format(q(name), sql.SQL(", ").join(cols)))
            cur.execute(sql.SQL("CREATE INDEX IF NOT EXISTS {} ON {} ({})").format(
                q(f"ix_{name}_{spec['key']}"), q(name), q(spec["key"])))
            # data month of the upload (YYYY-MM): every month's data is kept, a re-upload replaces only that month
            cur.execute(sql.SQL("ALTER TABLE {} ADD COLUMN IF NOT EXISTS {} TEXT").format(q(name), q("_period")))
            cur.execute(sql.SQL("CREATE INDEX IF NOT EXISTS {} ON {} ({})").format(
                q(f"ix_{name}__period"), q(name), q("_period")))
        cur.execute(HELPER_FUNCTIONS)
        cur.execute(RUN_TABLES)


HOUSEKEEPING = ("_row_no", "_source_file", "_period")


def existing_columns(cur, table):
    cur.execute("""SELECT column_name FROM information_schema.columns
                   WHERE table_schema = 'public' AND table_name = %s ORDER BY ordinal_position""", (table,))
    return [r["column_name"] for r in cur.fetchall()]


def add_columns(cur, table, columns):
    for c in columns:
        cur.execute(sql.SQL("ALTER TABLE {} ADD COLUMN IF NOT EXISTS {} TEXT").format(q(table), q(c)))


def delete_period(cur, table, period):
    cur.execute(sql.SQL("DELETE FROM {} WHERE {} = %s").format(q(table), q("_period")), (period,))


def truncate(cur, table):
    cur.execute(sql.SQL("TRUNCATE TABLE {}").format(q(table)))


def copy_rows(cur, table, columns, rows):
    """Bulk insert with COPY. rows = iterable of lists aligned with columns."""
    buf = io.StringIO()
    w = csv.writer(buf, lineterminator="\n")
    for r in rows:
        w.writerow(r)
    buf.seek(0)
    stmt = sql.SQL("COPY {} ({}) FROM STDIN WITH (FORMAT csv)").format(
        q(table), sql.SQL(", ").join(q(c) for c in columns))
    with cur.copy(stmt) as cp:
        cp.write(buf.read())


def table_counts():
    with cursor(commit=False) as cur:
        out = {}
        for name in DATASETS:
            cur.execute(sql.SQL("SELECT count(*) AS n, count(DISTINCT {}) AS f FROM {}").format(
                q("_source_file"), q(name)))
            r = cur.fetchone()
            out[name] = {"rows": r["n"], "files": r["f"]}
            cur.execute(sql.SQL("""SELECT coalesce(nullif({p}, ''), '(no month)') AS period, count(*) AS n
                                   FROM {t} GROUP BY 1 ORDER BY 1""").format(p=q("_period"), t=q(name)))
            out[name]["periods"] = {x["period"]: x["n"] for x in cur.fetchall()}
        cur.execute("SELECT * FROM upload_log ORDER BY loaded_at DESC LIMIT 20")
        log = cur.fetchall()
    return out, log


def loaded_periods():
    """All data months present in any dataset table, newest first."""
    with cursor(commit=False) as cur:
        parts = sql.SQL(" UNION ").join(sql.SQL("SELECT DISTINCT {} AS p FROM {}").format(q("_period"), q(n))
                                       for n in DATASETS)
        cur.execute(sql.SQL("SELECT p FROM ({}) x WHERE p IS NOT NULL AND p <> '' ORDER BY p DESC").format(parts))
        return [r["p"] for r in cur.fetchall()]
