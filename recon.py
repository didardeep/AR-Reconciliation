"""Reconciliation engine.

Replicates the three tested pgAdmin queries:
  1. net_sales      = Forward shipment_value - RT shipment_value (if packet in RT) else - RTO shipment_value, by packet_id
  2. net_settlement = Forward customer_paid_amount - Reverse customer_paid_amount, by order_release_id
  3. comparison     = net_sales joined to net_settlement ON packet_id = order_release_id

Differences from the raw SQL screenshots (deliberate, per the business rule described):
  * the SQL subtracted BOTH rt and rto; here RT is applied first and RTO only when the packet is not in RT
    (packets in both are flagged as an exception).
  * the SQL used exact inequality (<>); here a configurable tolerance decides MATCHED vs UNMATCHED.
  * a FULL OUTER JOIN is used so "sales not in settlement" / "settlement not in sales" are captured, not lost.

Column names: the names in the description above (packet_id, shipment_value, order_release_id,
customer_paid_amount, ...) are only the standard ones. The columns actually read from the five files are the
ones chosen on the Run screen (mapping.py); they are placed in the SQL below as {placeholders}, filled with
quoted identifiers. The net_sales / net_settlement / recon_results tables keep their own fixed column names.
"""
import logging
import time
from datetime import date

from psycopg2 import sql

import db
import hygiene
import mapping as colmap
from schema import DATASETS

log = logging.getLogger(__name__)


def _index_keys(cur, m):
    """Index the mapped join columns so the joins/group-bys don't scan full tables repeatedly."""
    for d in ("sales_forward", "sales_rt", "sales_rto", "settlement_forward", "settlement_reverse"):
        c = m.name(d, "key")
        if c:
            cur.execute(sql.SQL("CREATE INDEX IF NOT EXISTS {} ON {} ({})").format(
                sql.Identifier(("ix_" + d + "_" + "".join(ch if ch.isalnum() else "_" for ch in c))[:63]), db.q(d), db.q(c)))
        cur.execute(sql.SQL("ANALYZE {}").format(db.q(d)))


def _step(name, fn, *args):
    t = time.time()
    log.info("Run step '%s' started", name)
    fn(*args)
    log.info("Run step '%s' done in %.1fs", name, time.time() - t)


# code -> (readable name, what it means) for the reconciliation exceptions
EXCEPTIONS = {
    "SHORT_SETTLEMENT": ("Short settlement", "Net settlement is lower than net sales by more than the tolerance."),
    "EXCESS_SETTLEMENT": ("Excess settlement", "Net settlement is higher than net sales by more than the tolerance."),
    "SALES_NOT_IN_SETTLEMENT": ("Sales not in settlement", "Sales key has no row in Settlement - Forward."),
    "SETTLEMENT_NOT_IN_SALES": ("Settlement not in sales", "Settlement key has no row in Sales - Forward."),
    "PACKET_ID_NOT_EQUAL_ORDER_RELEASE_ID": ("Packet ID not equal to order release ID",
                                             "Inside the settlement file the Packet ID column differs from the order release ID."),
    "REVERSE_SETTLEMENT_WITHOUT_FORWARD": ("Reverse settlement without forward settlement",
                                           "Reverse settlement row whose order release ID is not in Settlement - Forward."),
    "MULTIPLE_REVERSE_SETTLEMENTS": ("Multiple reverse settlements for one order",
                                     "More than one reverse settlement row for the same order release ID."),
    "RETURN_IN_SALES_BUT_NO_REVERSE_SETTLEMENT": ("Return in sales but no reverse settlement",
                                                  "Sales show an RT / RTO return but there is no reverse settlement."),
    "REVERSE_SETTLEMENT_BUT_NO_RETURN_IN_SALES": ("Reverse settlement but no return in sales",
                                                  "A reverse settlement exists but sales have no RT / RTO row."),
    "COMMISSION_VARIANCE": ("Commission variance",
                            "Commission amount differs from settlement amount x commission % by more than the tolerance."),
    "TAX_VARIANCE": ("Tax variance",
                     "IGST + CGST + SGST differs from taxable amount x total tax rate by more than the tolerance."),
}


def _platform_filter(alias, platform, column):
    if platform:
        return sql.SQL(" AND {}.{} = %s").format(sql.SQL(alias), column), [platform]
    return sql.SQL(""), []


def build_net_sales(cur, m):
    cur.execute("DROP TABLE IF EXISTS net_sales")
    cur.execute(sql.SQL("""
        CREATE TABLE net_sales AS
        SELECT
            f.{fk}                                                      AS packet_id,
            try_numeric(f.{fa})                                         AS forward_sales,
            COALESCE(rt.rt_sales, 0)                                    AS rt_sales,
            COALESCE(rto.rto_sales, 0)                                  AS rto_sales,
            CASE WHEN rt.packet_id IS NOT NULL THEN 'RT'
                 WHEN rto.packet_id IS NOT NULL THEN 'RTO'
                 ELSE 'NONE' END                                        AS return_source,
            try_numeric(f.{fa})
              - CASE WHEN rt.packet_id IS NOT NULL THEN COALESCE(rt.rt_sales, 0)
                     ELSE COALESCE(rto.rto_sales, 0) END                AS net_sales
        FROM sales_forward f
        LEFT JOIN (
            SELECT {rtk} AS packet_id, SUM(try_numeric({rta})) AS rt_sales
            FROM sales_rt GROUP BY {rtk}
        ) rt  ON f.{fk} = rt.packet_id
        LEFT JOIN (
            SELECT {rtok} AS packet_id, SUM(try_numeric({rtoa})) AS rto_sales
            FROM sales_rto GROUP BY {rtok}
        ) rto ON f.{fk} = rto.packet_id
        WHERE f.{fk} IS NOT NULL AND btrim(f.{fk}) <> ''
    """).format(
        fk=m.col("sales_forward", "key"), fa=m.col("sales_forward", "amount"),
        rtk=m.col("sales_rt", "key"), rta=m.col("sales_rt", "amount"),
        rtok=m.col("sales_rto", "key"), rtoa=m.col("sales_rto", "amount")))
    cur.execute("CREATE INDEX ON net_sales(packet_id)")


def build_net_settlement(cur, platform, m):
    f_filter, f_params = _platform_filter("f", platform, m.col("settlement_forward", "platform"))
    r_filter, r_params = _platform_filter("r0", platform, m.col("settlement_reverse", "platform"))
    cur.execute("DROP TABLE IF EXISTS net_settlement")
    cur.execute(sql.SQL("""
        CREATE TABLE net_settlement AS
        SELECT
            f.{fk}                                                      AS order_release_id,
            try_numeric(f.{fa})                                         AS forward_amount,
            COALESCE(r.reverse_amount, 0)                               AS reverse_amount,
            COALESCE(r.n_reverse, 0)                                    AS n_reverse,
            try_numeric(f.{fa}) - COALESCE(r.reverse_amount, 0)         AS net_settlement
        FROM settlement_forward f
        LEFT JOIN (
            SELECT r0.{rk} AS order_release_id,
                   SUM(try_numeric(r0.{ra})) AS reverse_amount,
                   count(*) AS n_reverse
            FROM settlement_reverse r0
            WHERE 1=1 {rf}
            GROUP BY r0.{rk}
        ) r ON f.{fk} = r.order_release_id
        WHERE f.{fk} IS NOT NULL AND btrim(f.{fk}) <> '' {ff}
    """).format(rf=r_filter, ff=f_filter,
                fk=m.col("settlement_forward", "key"), fa=m.col("settlement_forward", "amount"),
                rk=m.col("settlement_reverse", "key"), ra=m.col("settlement_reverse", "amount")),
        r_params + f_params)
    cur.execute("CREATE INDEX ON net_settlement(order_release_id)")


def build_results(cur, run_id, tolerance):
    cur.execute("""
        INSERT INTO recon_results (run_id, packet_id, order_release_id, forward_sales, return_amount, return_source,
                                   net_sales, forward_settlement, reverse_settlement, net_settlement, difference, status)
        SELECT %s,
               s.packet_id, t.order_release_id,
               s.forward_sales,
               CASE s.return_source WHEN 'RT' THEN s.rt_sales WHEN 'RTO' THEN s.rto_sales ELSE 0 END,
               s.return_source,
               s.net_sales,
               t.forward_amount, t.reverse_amount, t.net_settlement,
               COALESCE(s.net_sales, 0) - COALESCE(t.net_settlement, 0)  AS difference,
               CASE WHEN s.packet_id IS NULL        THEN 'SETTLEMENT_NOT_IN_SALES'
                    WHEN t.order_release_id IS NULL THEN 'SALES_NOT_IN_SETTLEMENT'
                    WHEN abs(COALESCE(s.net_sales, 0) - COALESCE(t.net_settlement, 0)) <= %s THEN 'MATCHED'
                    ELSE 'UNMATCHED' END AS status
        FROM net_sales s
        FULL OUTER JOIN net_settlement t ON s.packet_id = t.order_release_id
    """, (run_id, tolerance))


INS = """INSERT INTO recon_exceptions
    (run_id, category, exception_type, dataset, row_no, packet_id, order_release_id, amount_1, amount_2, difference, description) """


def build_recon_exceptions(cur, run_id, tolerance, commission_tol, platform, m):
    log = hygiene.CheckLog(cur, run_id, "RECONCILIATION", EXCEPTIONS)
    cur.execute("SELECT count(*) AS n FROM recon_results WHERE run_id = %s", (run_id,))
    n_results = cur.fetchone()["n"]

    # Short / excess settlement (from UNMATCHED results)
    cur.execute(INS + """
        SELECT run_id, 'RECONCILIATION',
               CASE WHEN net_settlement < net_sales THEN 'SHORT_SETTLEMENT' ELSE 'EXCESS_SETTLEMENT' END,
               NULL, NULL, packet_id, order_release_id, net_sales, net_settlement, difference,
               CASE WHEN net_settlement < net_sales
                    THEN 'Settled ' || round(net_settlement,2) || ' against net sales ' || round(net_sales,2) || ' (short by ' || round(difference,2) || ')'
                    ELSE 'Settled ' || round(net_settlement,2) || ' against net sales ' || round(net_sales,2) || ' (excess of ' || round(-difference,2) || ')' END
        FROM recon_results WHERE run_id = %s AND status = 'UNMATCHED'""", (run_id,))
    log.add("SHORT_SETTLEMENT", None, columns="Net sales vs net settlement", checked=n_results)
    log.add("EXCESS_SETTLEMENT", None, columns="Net sales vs net settlement", checked=n_results)

    # Sales without settlement / settlement without sales
    cur.execute(INS + """
        SELECT run_id, 'RECONCILIATION', 'SALES_NOT_IN_SETTLEMENT', 'sales_forward', NULL, packet_id, NULL,
               net_sales, NULL, net_sales,
               'packet_id ' || packet_id || ' (net sales ' || round(net_sales,2) || ') has no order_release_id in Settlement - Forward'
        FROM recon_results WHERE run_id = %s AND status = 'SALES_NOT_IN_SETTLEMENT'""", (run_id,))
    log.add("SALES_NOT_IN_SETTLEMENT", "sales_forward", columns="Net sales vs net settlement", checked=n_results)
    cur.execute(INS + """
        SELECT run_id, 'RECONCILIATION', 'SETTLEMENT_NOT_IN_SALES', 'settlement_forward', NULL, NULL, order_release_id,
               NULL, net_settlement, -net_settlement,
               'order_release_id ' || order_release_id || ' (net settlement ' || round(net_settlement,2) || ') has no packet_id in Sales - Forward'
        FROM recon_results WHERE run_id = %s AND status = 'SETTLEMENT_NOT_IN_SALES'""", (run_id,))
    log.add("SETTLEMENT_NOT_IN_SALES", "settlement_forward", columns="Net sales vs net settlement", checked=n_results)

    # packet_id column inside the settlement file does not equal order_release_id
    for ds in ("settlement_forward", "settlement_reverse"):
        pref, key = m.col(ds, "packet_ref"), m.col(ds, "key")
        if not pref:
            log.add("PACKET_ID_NOT_EQUAL_ORDER_RELEASE_ID", ds, skipped="No 'Packet ID in settlement file' column mapped")
            continue
        cur.execute(sql.SQL(INS + """
            SELECT %s, 'RECONCILIATION', 'PACKET_ID_NOT_EQUAL_ORDER_RELEASE_ID', %s, t."_row_no", t.{p}, t.{k},
                   NULL, NULL, NULL,
                   'packet_id ' || coalesce(t.{p},'<blank>') || ' <> order_release_id ' || coalesce(t.{k},'<blank>') || ' in ' || %s
            FROM {tbl} t
            WHERE btrim(coalesce(t.{p},'')) <> '' AND btrim(coalesce(t.{k},'')) <> ''
              AND btrim(t.{p}) <> btrim(t.{k})""").format(tbl=db.q(ds), p=pref, k=key),
            (run_id, ds, DATASETS[ds]["label"]))
        log.add("PACKET_ID_NOT_EQUAL_ORDER_RELEASE_ID", ds, columns=[m.name(ds, "packet_ref"), m.name(ds, "key")])

    # Reverse settlement with no forward settlement
    fk, fa, fpref = (m.col("settlement_forward", r) for r in ("key", "amount", "packet_ref"))
    rk, ra, rpref = (m.col("settlement_reverse", r) for r in ("key", "amount", "packet_ref"))
    r_filter, r_params = _platform_filter("r", platform, m.col("settlement_reverse", "platform"))
    f_filter, f_params = _platform_filter("f0", platform, m.col("settlement_forward", "platform"))
    cur.execute(sql.SQL(INS + """
        SELECT %s, 'RECONCILIATION', 'REVERSE_SETTLEMENT_WITHOUT_FORWARD', 'settlement_reverse', r."_row_no",
               {rp}, r.{rk}, NULL, try_numeric(r.{ra}), NULL,
               'Reverse settlement of ' || coalesce(r.{ra},'') || ' for order_release_id ' || r.{rk} || ' has no forward settlement row'
        FROM settlement_reverse r
        LEFT JOIN (SELECT DISTINCT f0.{fk} AS order_release_id FROM settlement_forward f0 WHERE 1=1 {ff}) f
               ON f.order_release_id = r.{rk}
        WHERE f.order_release_id IS NULL AND btrim(coalesce(r.{rk},'')) <> '' {rf}""").format(
        ff=f_filter, rf=r_filter, fk=fk, rk=rk, ra=ra,
        rp=sql.SQL("r.{}").format(rpref) if rpref else sql.SQL("NULL")), [run_id] + f_params + r_params)
    log.add("REVERSE_SETTLEMENT_WITHOUT_FORWARD", "settlement_reverse",
            columns=[m.name("settlement_reverse", "key"), f"Settlement - Forward.{m.name('settlement_forward', 'key')}"])

    # Multiple reverse settlements for one order
    cur.execute(INS + """
        SELECT %s, 'RECONCILIATION', 'MULTIPLE_REVERSE_SETTLEMENTS', 'settlement_forward', NULL, NULL, order_release_id,
               forward_amount, reverse_amount, NULL,
               n_reverse || ' reverse settlement rows totalling ' || round(reverse_amount,2) || ' against forward ' || round(forward_amount,2)
        FROM net_settlement WHERE n_reverse > 1""", (run_id,))
    log.add("MULTIPLE_REVERSE_SETTLEMENTS", "settlement_forward", columns=m.name("settlement_reverse", "key"))

    # Forward settlement with no reverse although the sale has a return (RT/RTO) - and vice versa
    cur.execute(INS + """
        SELECT %s, 'RECONCILIATION', 'RETURN_IN_SALES_BUT_NO_REVERSE_SETTLEMENT', NULL, NULL, s.packet_id, t.order_release_id,
               CASE s.return_source WHEN 'RT' THEN s.rt_sales ELSE s.rto_sales END, t.reverse_amount, NULL,
               'Sales show a ' || s.return_source || ' return but Settlement - Reverse has no row for this order'
        FROM net_sales s JOIN net_settlement t ON s.packet_id = t.order_release_id
        WHERE s.return_source <> 'NONE' AND t.n_reverse = 0""", (run_id,))
    log.add("RETURN_IN_SALES_BUT_NO_REVERSE_SETTLEMENT", None, columns="Net sales vs net settlement", checked=n_results)
    cur.execute(INS + """
        SELECT %s, 'RECONCILIATION', 'REVERSE_SETTLEMENT_BUT_NO_RETURN_IN_SALES', NULL, NULL, s.packet_id, t.order_release_id,
               0, t.reverse_amount, NULL,
               'Settlement - Reverse has ' || t.n_reverse || ' row(s) totalling ' || round(t.reverse_amount,2) || ' but no RT/RTO row exists in sales'
        FROM net_sales s JOIN net_settlement t ON s.packet_id = t.order_release_id
        WHERE s.return_source = 'NONE' AND t.n_reverse > 0""", (run_id,))
    log.add("REVERSE_SETTLEMENT_BUT_NO_RETURN_IN_SALES", None, columns="Net sales vs net settlement", checked=n_results)

    # Re-performed commission and tax on forward settlement (BRD: accurate computations for commissions, taxes)
    fp = sql.SQL("f.{}").format(fpref) if fpref else sql.SQL("NULL")
    comm, pct = m.col("settlement_forward", "commission_amount"), m.col("settlement_forward", "commission_pct")
    names = {r: m.name("settlement_forward", r) for r in
             ("amount", "commission_amount", "commission_pct", "taxable_amount", "tax_rate", "igst", "cgst", "sgst")}
    if comm and pct:
        cur.execute(sql.SQL(INS + """
            SELECT %s, 'RECONCILIATION', 'COMMISSION_VARIANCE', 'settlement_forward', f."_row_no", {fp}, f.{fk},
                   try_numeric(f.{comm}),
                   round(try_numeric(f.{fa}) * try_numeric(f.{pct}) / 100.0, 2),
                   try_numeric(f.{comm}) - round(try_numeric(f.{fa}) * try_numeric(f.{pct}) / 100.0, 2),
                   %s || f.{comm} || ' vs recomputed ' ||
                   round(try_numeric(f.{fa}) * try_numeric(f.{pct}) / 100.0, 2) || %s
            FROM settlement_forward f
            WHERE try_numeric(f.{comm}) IS NOT NULL AND try_numeric(f.{pct}) IS NOT NULL
              AND try_numeric(f.{fa}) IS NOT NULL
              AND abs(try_numeric(f.{comm})
                      - round(try_numeric(f.{fa}) * try_numeric(f.{pct}) / 100.0, 2)) > %s""").format(
            fp=fp, fk=fk, fa=fa, comm=comm, pct=pct),
            (run_id, f"{names['commission_amount']} ", f" ({names['amount']} x {names['commission_pct']})", commission_tol))
        log.add("COMMISSION_VARIANCE", "settlement_forward",
                columns=[names["commission_amount"], names["amount"], names["commission_pct"]])
    else:
        log.add("COMMISSION_VARIANCE", "settlement_forward", skipped="No 'Commission amount' / 'Commission %' column mapped")

    taxable, rate = m.col("settlement_forward", "taxable_amount"), m.col("settlement_forward", "tax_rate")
    tax_parts = [m.col("settlement_forward", r) for r in ("igst", "cgst", "sgst")]
    tax_parts = [c for c in tax_parts if c]
    if taxable and rate and tax_parts:
        tax_sum = sql.SQL(" + ").join(sql.SQL("COALESCE(try_numeric(f.{}),0)").format(c) for c in tax_parts)
        cur.execute(sql.SQL(INS + """
            SELECT %s, 'RECONCILIATION', 'TAX_VARIANCE', 'settlement_forward', f."_row_no", {fp}, f.{fk},
                   {tax},
                   round(try_numeric(f.{taxable}) * try_numeric(f.{rate}) / 100.0, 2),
                   ({tax})
                     - round(try_numeric(f.{taxable}) * try_numeric(f.{rate}) / 100.0, 2),
                   'igst+cgst+sgst = ' ||
                   ({tax}) ||
                   %s || round(try_numeric(f.{taxable}) * try_numeric(f.{rate}) / 100.0, 2)
            FROM settlement_forward f
            WHERE try_numeric(f.{taxable}) IS NOT NULL AND try_numeric(f.{rate}) IS NOT NULL
              AND abs(({tax})
                      - round(try_numeric(f.{taxable}) * try_numeric(f.{rate}) / 100.0, 2)) > %s""").format(
            fp=fp, fk=fk, tax=tax_sum, taxable=taxable, rate=rate),
            (run_id, f" vs {names['taxable_amount']} x {names['tax_rate']} = ", commission_tol))
        log.add("TAX_VARIANCE", "settlement_forward",
                columns=[names["igst"], names["cgst"], names["sgst"], names["taxable_amount"], names["tax_rate"]])
    else:
        log.add("TAX_VARIANCE", "settlement_forward",
                skipped="No 'Taxable amount' / 'Total tax rate %' / tax amount column mapped")


def run_reconciliation(recon_month: date, tolerance: float, return_window: int, commission_tol: float, platform: str,
                       mapping=None):
    """Full pipeline. Returns run_id.

    mapping: mapping.Mapping chosen on the Run screen. None = the standard column names.
    """
    with db.cursor() as cur:
        m = mapping or colmap.default_mapping(cur)
        cur.execute("""INSERT INTO recon_runs (recon_month, tolerance, return_window, commission_tol, platform_filter, column_mapping)
                       VALUES (%s,%s,%s,%s,%s,%s) RETURNING run_id""",
                    (recon_month, tolerance, return_window, commission_tol, platform or None, m.to_json()))
        run_id = cur.fetchone()["run_id"]
        try:
            # fail with a clear error instead of hanging when another session (pgAdmin, an old run) holds a lock
            cur.execute("SET lock_timeout = '30s'")
            _step("normalise ids", colmap.normalise_ids, cur, m)
            _step("index keys", _index_keys, cur, m)
            t = time.time()
            log.info("Run step 'hygiene' started")
            hyg = hygiene.run_hygiene(cur, run_id, recon_month, return_window, m)
            log.info("Run step 'hygiene' done in %.1fs", time.time() - t)
            _step("net sales", build_net_sales, cur, m)
            _step("net settlement", build_net_settlement, cur, platform, m)
            _step("results", build_results, cur, run_id, tolerance)
            _step("recon exceptions", build_recon_exceptions, cur, run_id, tolerance, commission_tol, platform, m)

            cur.execute("""
                UPDATE recon_runs SET
                    status = 'DONE',
                    total_sales      = (SELECT count(*) FROM net_sales),
                    total_settlement = (SELECT count(*) FROM net_settlement),
                    matched          = (SELECT count(*) FROM recon_results WHERE run_id=%s AND status='MATCHED'),
                    unmatched        = (SELECT count(*) FROM recon_results WHERE run_id=%s AND status='UNMATCHED'),
                    sales_only       = (SELECT count(*) FROM recon_results WHERE run_id=%s AND status='SALES_NOT_IN_SETTLEMENT'),
                    settlement_only  = (SELECT count(*) FROM recon_results WHERE run_id=%s AND status='SETTLEMENT_NOT_IN_SALES'),
                    exception_count  = (SELECT count(*) FROM recon_exceptions WHERE run_id=%s AND category='RECONCILIATION'),
                    hygiene_count    = %s
                WHERE run_id = %s""", (run_id, run_id, run_id, run_id, run_id, hyg, run_id))
        except Exception as e:
            cur.connection.rollback()   # discards partial results of this run
            with db.cursor() as c2:
                c2.execute("""INSERT INTO recon_runs (recon_month, tolerance, return_window, commission_tol, platform_filter, status, message)
                              VALUES (%s,%s,%s,%s,%s,'FAILED',%s)""",
                           (recon_month, tolerance, return_window, commission_tol, platform or None, str(e)[:2000]))
            raise
    return run_id
