"""Date / amount metadata of every dataset per data month - to catch a wrong or partial upload BEFORE a run.

For each (dataset, month) it records: rows, blank and unreadable dates, min / max date, rows before / in / after
the month, total amount and the number of rows per day. It uses the column mapped as 'Period date' and the
amount column on the Run screen (the standard column when nothing has been mapped yet).

Flags (see flags()):
  * wrong month       - no row of the file falls in its data month
  * partial month     - data starts late / ends early (more than SETTLEMENT_LAG_DAYS + 1 days short)
  * missing days      - days of the month with no rows at all
  * outside the month - more than OUTSIDE_MONTH_WARN_PCT % of rows dated outside the month
                        (settlement files may run SETTLEMENT_LAG_DAYS into the next month: that is expected)
  * unreadable dates  - dates that are filled in but cannot be read
"""
import calendar
import json
from datetime import date, datetime, timedelta

from psycopg import sql

import config
import db
import mapping as colmap
from schema import DATASET_ORDER, DATASETS


def _columns(cur, dataset):
    """(date column, amount column) to profile: the saved mapping, else the standard column if it exists."""
    saved = colmap.load_saved(cur).get(dataset, {})
    table_cols = db.existing_columns(cur, dataset)
    out = []
    for role in ("period_date", "amount"):
        c = saved.get(role) or colmap._match(colmap.field(dataset, role)["default"], table_cols)
        out.append(c if c in table_cols else None)
    return out


def month_bounds(period):
    y, mth = int(period[:4]), int(period[5:7])
    return date(y, mth, 1), date(y, mth, calendar.monthrange(y, mth)[1])


def refresh(cur, dataset, period):
    """Recompute the profile of one dataset / month (called after every upload)."""
    dcol, acol = _columns(cur, dataset)
    first, last = month_bounds(period)
    tbl, p = db.q(dataset), db.q("_period")
    cur.execute(sql.SQL("SELECT count(*) AS n FROM {} WHERE {} = %s").format(tbl, p), (period,))
    rows = cur.fetchone()["n"]
    if rows == 0:
        cur.execute("DELETE FROM data_profile WHERE dataset = %s AND period = %s", (dataset, period))
        return
    d = sql.SQL("try_date({})").format(db.q(dcol)) if dcol else sql.SQL("NULL::date")
    raw = sql.SQL("btrim(coalesce({}, ''))").format(db.q(dcol)) if dcol else sql.SQL("''")
    a = sql.SQL("try_numeric({})").format(db.q(acol)) if acol else sql.SQL("NULL::numeric")
    cur.execute(sql.SQL("""
        SELECT count(*) FILTER (WHERE {raw} = '')                        AS blank_dates,
               count(*) FILTER (WHERE {raw} <> '' AND {d} IS NULL)        AS bad_dates,
               min({d}) AS min_date, max({d}) AS max_date,
               count(*) FILTER (WHERE {d} < %(f)s)                        AS before_month,
               count(*) FILTER (WHERE {d} BETWEEN %(f)s AND %(l)s)        AS in_month,
               count(*) FILTER (WHERE {d} > %(l)s)                        AS after_month,
               sum({a})                                                   AS total_amount
        FROM {tbl} WHERE {p} = %(per)s""").format(raw=raw, d=d, a=a, tbl=tbl, p=p),
        {"f": first, "l": last, "per": period})
    r = cur.fetchone()
    per_day = {}
    if dcol:
        cur.execute(sql.SQL("SELECT {d} AS day, count(*) AS n FROM {tbl} WHERE {p} = %s AND {d} IS NOT NULL "
                            "GROUP BY 1 ORDER BY 1").format(d=d, tbl=tbl, p=p), (period,))
        per_day = {x["day"].isoformat(): x["n"] for x in cur.fetchall()}
    cur.execute("""INSERT INTO data_profile (dataset, period, date_column, amount_column, rows, blank_dates, bad_dates,
                                             min_date, max_date, before_month, in_month, after_month, total_amount,
                                             per_day, computed_at)
                   VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s, now())
                   ON CONFLICT (dataset, period) DO UPDATE SET
                       date_column = EXCLUDED.date_column, amount_column = EXCLUDED.amount_column, rows = EXCLUDED.rows,
                       blank_dates = EXCLUDED.blank_dates, bad_dates = EXCLUDED.bad_dates,
                       min_date = EXCLUDED.min_date, max_date = EXCLUDED.max_date,
                       before_month = EXCLUDED.before_month, in_month = EXCLUDED.in_month,
                       after_month = EXCLUDED.after_month, total_amount = EXCLUDED.total_amount,
                       per_day = EXCLUDED.per_day, computed_at = now()""",
                (dataset, period, dcol, acol, rows, r["blank_dates"], r["bad_dates"], r["min_date"], r["max_date"],
                 r["before_month"], r["in_month"], r["after_month"], r["total_amount"], json.dumps(per_day)))


def refresh_all(cur):
    """Recompute every dataset / month (e.g. after the column mapping changed)."""
    for d in DATASET_ORDER:
        cur.execute(sql.SQL("SELECT DISTINCT {p} AS p FROM {t} WHERE coalesce({p}, '') <> ''").format(
            p=db.q("_period"), t=db.q(d)))
        for x in [r["p"] for r in cur.fetchall()]:
            refresh(cur, d, x)
    cur.execute(sql.SQL("DELETE FROM data_profile dp WHERE NOT EXISTS (SELECT 1 FROM ({}) x WHERE x.d = dp.dataset "
                        "AND x.p = dp.period)").format(
        sql.SQL(" UNION ALL ").join(sql.SQL("SELECT DISTINCT {l} AS d, {p} AS p FROM {t}").format(
            l=sql.Literal(d), p=db.q("_period"), t=db.q(d)) for d in DATASET_ORDER)))


def flags(r):
    """[(level, message)] for one profile row. level = 'bad' | 'warn'."""
    out = []
    first, last = month_bounds(r["period"])
    lag = config.SETTLEMENT_LAG_DAYS
    if not r["date_column"]:
        return [("warn", "No date column mapped - dates not checked")]
    dated = (r["before_month"] or 0) + (r["in_month"] or 0) + (r["after_month"] or 0)
    if dated and not r["in_month"]:
        out.append(("bad", f"No row dated in {first:%b %Y} - wrong data month chosen at upload?"))
    if r["bad_dates"]:
        out.append(("warn", f"{r['bad_dates']:,} unreadable date(s)"))
    if r["blank_dates"]:
        out.append(("warn", f"{r['blank_dates']:,} blank date(s)"))
    if r["in_month"]:
        if r["min_date"] and r["min_date"] > first + timedelta(days=lag + 1):
            out.append(("warn", f"Starts late: first date {r['min_date']:%d %b}"))
        in_max = max((date.fromisoformat(k) for k in r["per_day"] if first.isoformat() <= k <= last.isoformat()),
                     default=None)
        if in_max and in_max < last - timedelta(days=lag + 1):
            out.append(("warn", f"Ends early: last date in the month {in_max:%d %b} ({(last - in_max).days} days short)"))
        missing = [first + timedelta(days=i) for i in range((last - first).days + 1)
                   if (first + timedelta(days=i)).isoformat() not in r["per_day"]]
        if missing and len(missing) < (last - first).days:
            shown = ", ".join(f"{x:%d}" for x in missing[:10]) + (" ..." if len(missing) > 10 else "")
            out.append(("warn", f"{len(missing)} day(s) with no rows: {shown}"))
    if dated:
        # settlement may run T+N days into next month - that part is expected
        expected_spill = 0
        if r["dataset"].startswith("settlement"):
            expected_spill = sum(n for k, n in r["per_day"].items()
                                 if last.isoformat() < k <= (last + timedelta(days=lag)).isoformat())
        outside = (r["before_month"] or 0) + (r["after_month"] or 0) - expected_spill
        pct = 100.0 * outside / dated
        if outside and pct > config.OUTSIDE_MONTH_WARN_PCT:
            out.append(("warn", f"{outside:,} rows ({pct:.1f}%) dated outside {first:%b %Y}"))
    return out


def load(cur):
    """{period: {dataset: profile row + flags}} for the upload screen / metadata page, newest month first."""
    cur.execute("SELECT * FROM data_profile ORDER BY period DESC")
    out = {}
    for r in cur.fetchall():
        r = dict(r)
        r["per_day"] = json.loads(r["per_day"] or "{}")
        r["flags"] = flags(r)
        out.setdefault(r["period"], {})[r["dataset"]] = r
    return out


def cross_checks(by_dataset, period):
    """Comparisons between the files of one month (sales vs settlement coverage)."""
    msgs = []
    s, t = by_dataset.get("sales_forward"), by_dataset.get("settlement_forward")
    if s and t and s["max_date"] and t["max_date"]:
        first, last = month_bounds(period)
        s_end = min(s["max_date"], last)
        t_end = min(t["max_date"], last + timedelta(days=config.SETTLEMENT_LAG_DAYS))
        if t_end < s_end:
            msgs.append(f"Settlement - Forward ends {t_end:%d %b} but Sales - Forward runs to {s_end:%d %b}: sales after "
                        f"{t_end:%d %b} will show as 'Sales not in settlement' until next month's settlement is loaded.")
        if s["total_amount"] and t["total_amount"]:
            gap = s["total_amount"] - t["total_amount"]
            msgs.append(f"Forward totals: sales {s['total_amount']:,.2f} vs settlement {t['total_amount']:,.2f} "
                        f"(gap {gap:,.2f}, before returns).")
    return msgs
