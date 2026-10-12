"""PostgreSQL access layer: connections, schema creation, safe-cast helper functions, bulk COPY load."""
import csv
import io
from contextlib import contextmanager

import psycopg2
import psycopg2.extras
from psycopg2 import sql

import config
from schema import DATASETS


def get_conn():
    return psycopg2.connect(config.DATABASE_URL)


@contextmanager
def cursor(commit=True):
    conn = get_conn()
    try:
        cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
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
CREATE OR REPLACE FUNCTION try_numeric(t text) RETURNS numeric AS $$
BEGIN
    IF t IS NULL OR btrim(t) = '' THEN RETURN NULL; END IF;
    RETURN replace(btrim(t), ',', '')::numeric;
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
        cur.execute(HELPER_FUNCTIONS)
        cur.execute(RUN_TABLES)


HOUSEKEEPING = ("_row_no", "_source_file")


def existing_columns(cur, table):
    cur.execute("""SELECT column_name FROM information_schema.columns
                   WHERE table_schema = 'public' AND table_name = %s ORDER BY ordinal_position""", (table,))
    return [r["column_name"] for r in cur.fetchall()]


def add_columns(cur, table, columns):
    for c in columns:
        cur.execute(sql.SQL("ALTER TABLE {} ADD COLUMN IF NOT EXISTS {} TEXT").format(q(table), q(c)))


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
    cur.copy_expert(stmt.as_string(cur.connection), buf)


def table_counts():
    with cursor(commit=False) as cur:
        out = {}
        for name in DATASETS:
            cur.execute(sql.SQL("SELECT count(*) AS n, count(DISTINCT {}) AS f FROM {}").format(
                q("_source_file"), q(name)))
            r = cur.fetchone()
            out[name] = {"rows": r["n"], "files": r["f"]}
        cur.execute("SELECT * FROM upload_log ORDER BY loaded_at DESC LIMIT 20")
        log = cur.fetchall()
    return out, log
