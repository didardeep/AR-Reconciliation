"""Month-by-month reconciliation with carry-forward of open items.

Every month's data stays in the dataset tables, tagged with its data month (_period = 'YYYY-MM').
A run for month M does NOT reconcile all history. It reconciles a *scope* of keys:

    scope = keys that have any row in month M (in any of the five files)
          + keys still open from earlier runs (open_items.is_open)

For those keys ALL their rows up to month M are used (a sale of August, its settlement of September and its
return of October all add up on the same key). This is done by creating, for the duration of the run, a
TEMP table with the same name as each dataset table, holding only the in-scope rows. Postgres looks in the
session's temp schema first, so the existing engine (hygiene.py / recon.py) reads the scoped data without any
change to its SQL.

After the results are built:
    * open items that are now Matched are closed (closed_month = M)
    * every key that is not Matched is (re)written to open_items, keeping the month it was first left open
    * a 'Sales not in settlement' key whose sale is in the last N days of the month (T+N settlement) is marked
      awaiting_settlement: it is expected to settle next month and is not an error yet.
"""
import calendar
from datetime import date, timedelta

from psycopg import sql

import config
import db
from schema import DATASET_ORDER

PERIOD = db.q("_period")


def month_span(run, fmt="%b %Y", sep=" - "):
    """'Feb 2024' or 'Feb 2024 - Mar 2024' for a recon_runs row (dict)."""
    a, b = run.get("recon_month"), run.get("recon_month_to")
    if not a:
        return ""
    return a.strftime(fmt) if not b or b == a else f"{a.strftime(fmt)}{sep}{b.strftime(fmt)}"


def period_of(month: date) -> str:
    return month.strftime("%Y-%m")


def _month_filter(alias, from_str, to_str):
    """Rows of the selected months (from..to); rows loaded before data months existed (no _period) count as every month."""
    return sql.SQL("(({a}.{p} >= {f} AND {a}.{p} <= {t}) OR coalesce({a}.{p}, '') = '')").format(
        a=sql.Identifier(alias), p=PERIOD, f=sql.Literal(from_str), t=sql.Literal(to_str))


def build_scope(cur, month: date, platform, m, month_to: date = None):
    """TEMP table scope_keys(k, carried): keys to reconcile in this run (months month..month_to)."""
    month_to = month_to or month
    ms, mt = period_of(month), period_of(month_to)
    cur.execute("DROP TABLE IF EXISTS scope_keys")
    cur.execute("CREATE TEMP TABLE scope_keys (k TEXT PRIMARY KEY, carried BOOLEAN NOT NULL DEFAULT false) ON COMMIT DROP")
    for d in DATASET_ORDER:
        key = m.col(d, "key")
        plat = m.col(d, "platform") if d.startswith("settlement") else None
        pf = (sql.SQL(" AND upper(btrim(t.{})) = upper(btrim({}))").format(plat, sql.Literal(platform))
              if (platform and plat) else sql.SQL(""))
        cur.execute(sql.SQL("""INSERT INTO scope_keys (k)
                               SELECT DISTINCT btrim(t.{k}) FROM public.{tbl} t
                               WHERE {mf} AND btrim(coalesce(t.{k}, '')) <> '' {pf}
                               ON CONFLICT DO NOTHING""").format(
            k=key, tbl=db.q(d), mf=_month_filter("t", ms, mt), pf=pf))
    # open items from EARLIER months are carried in
    cur.execute("""INSERT INTO scope_keys (k, carried)
                   SELECT item_key, true FROM open_items
                   WHERE is_open AND platform = %s AND first_month < %s
                   ON CONFLICT (k) DO UPDATE SET carried = true""", (platform or "", month))
    cur.execute("ANALYZE scope_keys")
    cur.execute("SELECT count(*) AS n, count(*) FILTER (WHERE carried) AS c FROM scope_keys")
    return cur.fetchone()


def shadow_tables(cur, month: date, m):
    """TEMP copies of the five dataset tables holding only in-scope keys, rows up to month M (the last selected month)."""
    ms = period_of(month)
    for d in DATASET_ORDER:
        key = m.col(d, "key")
        cur.execute(sql.SQL("DROP TABLE IF EXISTS pg_temp.{}").format(db.q(d)))
        cur.execute(sql.SQL("""CREATE TEMP TABLE {tbl} ON COMMIT DROP AS
                               SELECT t.* FROM public.{tbl} t
                               WHERE btrim(t.{k}) IN (SELECT k FROM scope_keys)
                                 AND (coalesce(t.{p}, '') = '' OR t.{p} <= {m})""").format(
            tbl=db.q(d), k=key, p=PERIOD, m=sql.Literal(ms)))
        cur.execute(sql.SQL("CREATE INDEX ON pg_temp.{} ({})").format(db.q(d), key))
        cur.execute(sql.SQL("ANALYZE pg_temp.{}").format(db.q(d)))


def _date_per_key(dataset, m, role):
    """SELECT key, max(date) for a mapped date role of a dataset (reads the scoped temp table)."""
    k, c = m.col(dataset, "key"), m.col(dataset, role)
    if not (k and c):
        return None
    return sql.SQL("SELECT btrim({k}) AS k, max(try_date({c})) AS d FROM {t} GROUP BY 1").format(
        k=k, c=c, t=db.q(dataset))


def update_open_items(cur, run_id, month: date, platform, m):
    """Close what matched, (re)open what did not. Returns the carry-forward counts for recon_runs.
    month = the LAST month of the run (for a two-month run, the second month)."""
    plat = platform or ""
    month_end = date(month.year, month.month, calendar.monthrange(month.year, month.month)[1])
    lag_from = month_end - timedelta(days=config.SETTLEMENT_LAG_DAYS - 1)

    # one line per key: not Matched wins if a key has several result rows
    cur.execute("DROP TABLE IF EXISTS run_keys")
    sale_q = _date_per_key("sales_forward", m, "period_date")
    ret_parts = [q for q in (_date_per_key("sales_rt", m, "return_date"), _date_per_key("sales_rto", m, "return_date")) if q]
    cur.execute(sql.SQL("""
        CREATE TEMP TABLE run_keys ON COMMIT DROP AS
        SELECT DISTINCT ON (r.k) r.k, r.status, r.net_sales, r.net_settlement, r.difference,
               sd.d AS sale_date, rd.d AS return_date
        FROM (SELECT coalesce(packet_id, order_release_id) AS k, status, net_sales, net_settlement, difference
              FROM recon_results WHERE run_id = %s
                -- a reverse settlement with no forward / sales is reported in the results but is not an open item
                AND status <> 'REVERSE_SETTLEMENT_WITHOUT_FORWARD') r
        LEFT JOIN ({sale}) sd ON sd.k = r.k
        LEFT JOIN (SELECT k, max(d) AS d FROM ({ret}) x GROUP BY k) rd ON rd.k = r.k
        ORDER BY r.k, (r.status = 'MATCHED'), abs(coalesce(r.difference, 0)) DESC""").format(
        sale=sale_q or sql.SQL("SELECT NULL::text AS k, NULL::date AS d"),
        ret=sql.SQL(" UNION ALL ").join(ret_parts) if ret_parts else sql.SQL("SELECT NULL::text AS k, NULL::date AS d")),
        (run_id,))
    # T+N: the counterpart is expected next month
    # two separate statements: psycopg 3 cannot send several commands in one execute() when params are bound
    cur.execute("ALTER TABLE run_keys ADD COLUMN awaiting BOOLEAN")
    cur.execute("""UPDATE run_keys SET awaiting =
                       (status = 'SALES_NOT_IN_SETTLEMENT' AND sale_date BETWEEN %(f)s AND %(e)s)
                    OR (status IN ('SALES_HIGHER_THAN_SETTLEMENT', 'SETTLEMENT_HIGHER_THAN_SALES',
                                   'RETURN_PROCESSED_SETTLEMENT_NOT_ADJUSTED')
                        AND return_date BETWEEN %(f)s AND %(e)s)""",
                {"f": lag_from, "e": month_end})

    # 1. close open items that matched now (a re-run of an OLDER month never touches items handled later)
    cur.execute("""UPDATE open_items o SET is_open = false, status = 'MATCHED', closed_month = %(m)s, closed_run_id = %(r)s,
                          last_month = %(m)s, last_run_id = %(r)s, net_sales = k.net_sales,
                          net_settlement = k.net_settlement, difference = k.difference, awaiting_settlement = false
                   FROM run_keys k
                   WHERE o.item_key = k.k AND o.platform = %(p)s AND o.is_open AND k.status = 'MATCHED'
                     AND o.last_month <= %(m)s""", {"m": month, "r": run_id, "p": plat})
    # 2. open / keep open everything else (re-opening a closed item if new data broke it)
    cur.execute("""INSERT INTO open_items (item_key, platform, first_month, first_run_id, last_month, last_run_id, status,
                                           net_sales, net_settlement, difference, sale_date, awaiting_settlement, is_open)
                   SELECT k, %(p)s, %(m)s, %(r)s, %(m)s, %(r)s, status, net_sales, net_settlement, difference,
                          sale_date, coalesce(awaiting, false), true
                   FROM run_keys WHERE status <> 'MATCHED'
                   ON CONFLICT (item_key, platform) DO UPDATE SET
                       first_month  = CASE WHEN open_items.is_open THEN least(open_items.first_month, EXCLUDED.first_month)
                                           ELSE EXCLUDED.first_month END,
                       first_run_id = CASE WHEN open_items.is_open AND open_items.first_month <= EXCLUDED.first_month
                                           THEN open_items.first_run_id ELSE EXCLUDED.first_run_id END,
                       last_month = EXCLUDED.last_month, last_run_id = EXCLUDED.last_run_id, status = EXCLUDED.status,
                       net_sales = EXCLUDED.net_sales, net_settlement = EXCLUDED.net_settlement,
                       difference = EXCLUDED.difference, sale_date = EXCLUDED.sale_date,
                       awaiting_settlement = EXCLUDED.awaiting_settlement, is_open = true,
                       closed_month = NULL, closed_run_id = NULL
                   WHERE open_items.last_month IS NULL OR open_items.last_month <= EXCLUDED.last_month""",
                {"m": month, "r": run_id, "p": plat})

    # 3. mark the results: carried from an earlier month + the month the key was first open
    cur.execute("""UPDATE recon_results r SET carried = s.carried, first_month = o.first_month
                   FROM scope_keys s LEFT JOIN open_items o ON o.item_key = s.k AND o.platform = %s
                   WHERE r.run_id = %s AND s.k = coalesce(r.packet_id, r.order_release_id)""", (plat, run_id))

    cur.execute("""SELECT count(*) FILTER (WHERE s.carried)                                AS carried_in,
                          count(*) FILTER (WHERE s.carried AND k.status = 'MATCHED')       AS closed_prior,
                          count(*) FILTER (WHERE NOT s.carried AND k.status <> 'MATCHED')  AS new_open,
                          count(*) FILTER (WHERE k.awaiting)                               AS awaiting
                   FROM scope_keys s LEFT JOIN run_keys k ON k.k = s.k""")
    out = dict(cur.fetchone())
    cur.execute("SELECT count(*) AS n FROM open_items WHERE is_open AND platform = %s", (plat,))
    out["open_total"] = cur.fetchone()["n"]
    return out


AGE_BUCKETS_SQL = """CASE WHEN age_days <= 30 THEN '0-30 days' WHEN age_days <= 60 THEN '31-60 days'
                          WHEN age_days <= 90 THEN '61-90 days' ELSE '90+ days' END"""


def open_items_query(platform=None, only_open=True):
    """SQL + params for the open-items list (with age in days as of today)."""
    where, params = [], []
    if only_open:
        where.append("is_open")
    if platform is not None:
        where.append("platform = %s")
        params.append(platform)
    w = ("WHERE " + " AND ".join(where)) if where else ""
    q = f"""SELECT *, {AGE_BUCKETS_SQL} AS age_bucket FROM (
                SELECT o.*, (current_date - coalesce(o.sale_date, o.first_month)) AS age_days FROM open_items o {w}) x
            ORDER BY awaiting_settlement, age_days DESC, abs(coalesce(difference, 0)) DESC"""
    return q, params
