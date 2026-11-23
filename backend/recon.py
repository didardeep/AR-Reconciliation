"""Reconciliation engine.

Replicates the three tested pgAdmin queries:
  1. net_sales      = SUM(forward shipment_value) - SUM(RT shipment_value) (if packet in RT) else - SUM(RTO), per packet_id
  2. net_settlement = SUM(forward customer_paid_amount) - SUM(reverse customer_paid_amount), per order_release_id
     (a packet / order can have several item rows: they are added up first, so each key gives ONE result row)
  3. comparison     = net_sales joined to net_settlement ON packet_id = order_release_id

Differences from the raw SQL screenshots (deliberate, per the business rule described):
  * the SQL subtracted BOTH rt and rto; here RT is applied first and RTO only when the packet is not in RT
    (packets in both are flagged as an exception).
  * the SQL used exact inequality (<>); here a configurable tolerance decides MATCHED, and a difference beyond it is
    split into SALES_HIGHER_THAN_SETTLEMENT (net sales > net settlement) and SETTLEMENT_HIGHER_THAN_SALES.
  * a FULL OUTER JOIN is used so "sales not in settlement" / "settlement not in sales" are captured, not lost.

Column names: the names in the description above (packet_id, shipment_value, order_release_id,
customer_paid_amount, ...) are only the standard ones. The columns actually read from the five files are the
ones chosen on the Run screen (mapping.py); they are placed in the SQL below as {placeholders}, filled with
quoted identifiers. The net_sales / net_settlement / recon_results tables keep their own fixed column names.
"""
import logging
import time
from datetime import date

from psycopg import sql

import carry
import config
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
# Status of every result row. MATCHED / SALES_HIGHER / SETTLEMENT_HIGHER split what used to be "Unmatched".
STATUSES = {
    "MATCHED": ("Matched", "|net sales - net settlement| is within the tolerance."),
    "SALES_HIGHER_THAN_SETTLEMENT": ("Sales higher than settlement",
                                     "Net sales is higher than net settlement by more than the tolerance (settled short)."),
    "SETTLEMENT_HIGHER_THAN_SALES": ("Settlement higher than sales",
                                     "Net settlement is higher than net sales by more than the tolerance (settled in excess)."),
    "RETURN_PROCESSED_SETTLEMENT_NOT_ADJUSTED": ("Return processed but settlement not adjusted",
                                                 "Net sales is 0 because the RT / RTO return is processed, but net "
                                                 "settlement is not 0 (settlement not adjusted for the return)."),
    "SALES_NOT_IN_SETTLEMENT": ("Sales not in settlement", "Sales key has no row in Settlement - Forward."),
    "SETTLEMENT_NOT_IN_SALES": ("Settlement not in sales", "Settlement key has no row in Sales - Forward."),
    "REVERSE_SETTLEMENT_WITHOUT_FORWARD": ("Reverse settlement without forward settlement",
                                           "Key exists only in Settlement - Reverse (no forward settlement, no sales)."),
}
# statuses that mean "amount differs" (old runs stored these as one status, UNMATCHED)
AMOUNT_DIFF_STATUSES = ("SALES_HIGHER_THAN_SETTLEMENT", "SETTLEMENT_HIGHER_THAN_SALES", "UNMATCHED",
                        "RETURN_PROCESSED_SETTLEMENT_NOT_ADJUSTED")

# code -> (readable name, what it means) for the reconciliation exceptions. They are no longer listed on a page
# of their own: each result row carries the exceptions of its key next to its status (recon_results.exceptions).
EXCEPTIONS = {
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
    "TAX_VARIANCE": ("Tax variance",
                     "IGST + CGST + SGST differs from taxable amount x total tax rate by more than the tolerance."),
    # ---- fee checks on the settlement files (difference = the excess / short amount, always positive)
    "COMMISSION_ABOVE_ALLOWED": ("Commission above allowed %",
                                 "|Total commission| / net sales (customer paid) x 100, per forward settlement row, is "
                                 "above the allowed commission %. Difference = commission charged - commission at the "
                                 "allowed %."),
    "LOGISTICS_ABOVE_ALLOWED": ("Logistics above allowed %",
                                "Forward + reverse logistics deduction of the order / MRP x 100 is above the allowed "
                                "logistics %. Reverse rows of every uploaded month are included. "
                                "Difference = logistics charged - logistics at the allowed %."),
    "FEE_BASE_MISSING": ("Base missing for fee check",
                         "Forward settlement row with commission but no net sales, or logistics but no MRP: the "
                         "commission % / logistics % cannot be calculated."),
    "LOGISTICS_COMPONENT_MISMATCH": ("Logistics components do not add up",
                                     "Shipping + fixed + pick and pack + payment gateway fee + tax on logistics differs "
                                     "from the total logistics deduction by more than the tolerance."),
    "LOGISTICS_TAX_VARIANCE": ("Tax on logistics variance",
                               "Tax on logistics differs from the GST rate x (shipping + fixed + pick and pack + "
                               "payment gateway fee) by more than the tolerance."),
    "REVERSE_LOGISTICS_MISSING": ("Reverse logistics missing",
                                  "Reverse settlement row with a blank / 0 logistics deduction (logistics is always "
                                  "charged on reverse)."),
    "REVERSE_HAS_COMMISSION": ("Commission on reverse settlement",
                               "Reverse settlement row with a commission (commission is not charged on reverse)."),
    "SETTLEMENT_SHORT_PAID": ("Settlement short paid",
                              "Total actual settlement is lower than customer paid - (commission + TCS + TDS) - logistics "
                              "by more than the tolerance: a deduction not explained by these columns."),
    "SETTLEMENT_EXCESS_PAID": ("Settlement excess paid",
                               "Total actual settlement is higher than customer paid - (commission + TCS + TDS) - "
                               "logistics by more than the tolerance."),
    "PENDING_SETTLEMENT_MISMATCH": ("Pending settlement does not match",
                                    "Amount pending settlement differs from expected - actual settlement (negative = "
                                    "paid in excess). Short paid with nothing pending = money missing."),
    "CLAIM_RECOVERABLE": ("Claim recoverable",
                          "Fair settlement (customer paid - commission capped at the allowed % of net sales - TCS - "
                          "TDS - logistics capped at the allowed % of MRP) is higher than the total actual settlement. "
                          "Difference = amount to claim back from the marketplace."),
    "PAYMENT_SPLIT_MISMATCH": ("Prepaid + postpaid does not equal actual settlement",
                               "Prepaid payment + postpaid payment differs from the total actual settlement by more "
                               "than the tolerance."),
    "AMOUNT_SPLIT_MISMATCH": ("Prepaid + postpaid amount does not equal customer paid",
                              "prepaid_amount + postpaid_amount differs from customer_paid_amt."),
    "TAXABLE_AMOUNT_VARIANCE": ("Taxable amount variance",
                                "taxable_amount differs from customer_paid_amt / (1 + total_tax_rate / 100)."),
    "COMMISSION_CALC_VARIANCE": ("Commission calculation variance",
                                 "platform_fees differs from customer_paid_amt x commission_percentage / 100, or "
                                 "total_commission differs from -platform_fees."),
    "COMMISSION_SPLIT_MISMATCH": ("Prepaid + postpaid commission does not equal total commission",
                                  "prepaid_total_commission + postpaid_total_commission differs from total_commission."),
    "TCS_TDS_VARIANCE": ("TCS / TDS variance",
                         "TCS differs from taxable amount x the TCS rate, TDS from taxable amount x the TDS rate, or "
                         "total_commission_plus_tcs_tds_deduction from total commission + TCS + TDS."),
    "TCS_TDS_SPLIT_MISMATCH": ("TCS / TDS prepaid-postpaid split variance",
                               "tcs / tds prepaid and postpaid amounts are not in proportion to prepaid_amount / "
                               "postpaid_amount of customer paid."),
    "PG_FEE_VARIANCE": ("Payment gateway fee variance", "payment_gateway_fee differs from customer paid x the payment "
                                                        "gateway rate."),
    "DEDUCTION_SPLIT_MISMATCH": ("Prepaid / postpaid deduction split variance",
                                 "Prepaid / postpaid commission and logistics deductions are not in proportion to "
                                 "prepaid_amount / postpaid_amount of customer paid."),
    "EXPECTED_SETTLEMENT_VARIANCE": ("Expected settlement variance",
                                     "The file's total_expected_settlement differs from customer paid + commission/TCS/"
                                     "TDS deduction + logistics deduction."),
    "PAYMENT_WITHOUT_UTR": ("Payment without bank UTR",
                            "A prepaid / postpaid payment amount with a blank bank UTR: no proof the money was sent."),
}

# fee checks shown as tiles on the dashboard (count + Rs of the 'difference' column)
FEE_CHECKS = ("COMMISSION_ABOVE_ALLOWED", "LOGISTICS_ABOVE_ALLOWED", "CLAIM_RECOVERABLE", "SETTLEMENT_SHORT_PAID",
              "SETTLEMENT_EXCESS_PAID", "PENDING_SETTLEMENT_MISMATCH", "PAYMENT_SPLIT_MISMATCH", "PAYMENT_WITHOUT_UTR",
              "LOGISTICS_COMPONENT_MISMATCH", "LOGISTICS_TAX_VARIANCE", "REVERSE_LOGISTICS_MISSING",
              "REVERSE_HAS_COMMISSION", "FEE_BASE_MISSING", "EXPECTED_SETTLEMENT_VARIANCE", "AMOUNT_SPLIT_MISMATCH",
              "TAXABLE_AMOUNT_VARIANCE", "COMMISSION_CALC_VARIANCE", "COMMISSION_SPLIT_MISMATCH", "TCS_TDS_VARIANCE",
              "TCS_TDS_SPLIT_MISMATCH", "PG_FEE_VARIANCE", "DEDUCTION_SPLIT_MISMATCH")


def _platform_filter(alias, platform, column):
    if platform:
        # compared ignoring case and spaces: "Myntra" = "MYNTRA "
        return sql.SQL(" AND upper(btrim({}.{})) = upper(btrim(%s))").format(sql.SQL(alias), column), [platform]
    return sql.SQL(""), []


def build_net_sales(cur, m):
    """One row per sales key: all forward rows of the packet added up (a packet can have several item rows),
    minus the RT return total if the packet is in RT, otherwise minus the RTO total.
    Return amounts are taken as absolute values, so files that store returns as negative numbers work too."""
    cur.execute("DROP TABLE IF EXISTS net_sales")
    cur.execute(sql.SQL("""
        CREATE TABLE net_sales AS
        SELECT
            f.packet_id,
            f.forward_sales,
            f.n_rows,
            COALESCE(rt.rt_sales, 0)                                    AS rt_sales,
            COALESCE(rto.rto_sales, 0)                                  AS rto_sales,
            CASE WHEN rt.packet_id IS NOT NULL THEN 'RT'
                 WHEN rto.packet_id IS NOT NULL THEN 'RTO'
                 ELSE 'NONE' END                                        AS return_source,
            f.forward_sales
              - CASE WHEN rt.packet_id IS NOT NULL THEN COALESCE(rt.rt_sales, 0)
                     ELSE COALESCE(rto.rto_sales, 0) END                AS net_sales
        FROM (
            SELECT btrim({fk}) AS packet_id, SUM(try_numeric({fa})) AS forward_sales, count(*) AS n_rows
            FROM sales_forward
            WHERE {fk} IS NOT NULL AND btrim({fk}) <> ''
            GROUP BY btrim({fk})
        ) f
        LEFT JOIN (
            SELECT btrim({rtk}) AS packet_id, SUM(abs(try_numeric({rta}))) AS rt_sales
            FROM sales_rt GROUP BY btrim({rtk})
        ) rt  ON f.packet_id = rt.packet_id
        LEFT JOIN (
            SELECT btrim({rtok}) AS packet_id, SUM(abs(try_numeric({rtoa}))) AS rto_sales
            FROM sales_rto GROUP BY btrim({rtok})
        ) rto ON f.packet_id = rto.packet_id
    """).format(
        fk=m.col("sales_forward", "key"), fa=m.col("sales_forward", "amount"),
        rtk=m.col("sales_rt", "key"), rta=m.col("sales_rt", "amount"),
        rtok=m.col("sales_rto", "key"), rtoa=m.col("sales_rto", "amount")))
    cur.execute("CREATE UNIQUE INDEX ON net_sales(packet_id)")


def build_net_settlement(cur, platform, m):
    """One row per settlement key: all forward settlement rows of the order added up, minus all reverse rows.
    A key that has a sale and a reverse settlement but NO forward settlement is included with forward = 0
    (net settlement = -reverse), so it is compared with its sale instead of showing as 'Sales not in settlement'.
    A reverse-only key with no sale stays out (it gets its own REVERSE_SETTLEMENT_WITHOUT_FORWARD row)."""
    f_filter, f_params = _platform_filter("f0", platform, m.col("settlement_forward", "platform"))
    r_filter, r_params = _platform_filter("r0", platform, m.col("settlement_reverse", "platform"))
    cur.execute("DROP TABLE IF EXISTS net_settlement")
    cur.execute(sql.SQL("""
        CREATE TABLE net_settlement AS
        SELECT
            COALESCE(f.order_release_id, r.order_release_id)           AS order_release_id,
            COALESCE(f.forward_amount, 0)                               AS forward_amount,
            COALESCE(f.n_rows, 0)                                       AS n_rows,
            COALESCE(r.reverse_amount, 0)                               AS reverse_amount,
            COALESCE(r.n_reverse, 0)                                    AS n_reverse,
            COALESCE(f.forward_amount, 0) - COALESCE(r.reverse_amount, 0) AS net_settlement
        FROM (
            SELECT btrim(f0.{fk}) AS order_release_id, SUM(try_numeric(f0.{fa})) AS forward_amount, count(*) AS n_rows
            FROM settlement_forward f0
            WHERE f0.{fk} IS NOT NULL AND btrim(f0.{fk}) <> '' {ff}
            GROUP BY btrim(f0.{fk})
        ) f
        FULL OUTER JOIN (
            SELECT btrim(r0.{rk}) AS order_release_id,
                   SUM(abs(try_numeric(r0.{ra}))) AS reverse_amount,   -- sign-safe: a reverse always reduces settlement
                   count(*) AS n_reverse
            FROM settlement_reverse r0
            WHERE 1=1 {rf}
            GROUP BY btrim(r0.{rk})
        ) r ON f.order_release_id = r.order_release_id
        -- forward settlement exists, or (reverse only) the key has a sale
        WHERE f.order_release_id IS NOT NULL
           OR r.order_release_id IN (SELECT packet_id FROM net_sales)
    """).format(rf=r_filter, ff=f_filter,
                fk=m.col("settlement_forward", "key"), fa=m.col("settlement_forward", "amount"),
                rk=m.col("settlement_reverse", "key"), ra=m.col("settlement_reverse", "amount")),
        f_params + r_params)
    cur.execute("CREATE UNIQUE INDEX ON net_settlement(order_release_id)")


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
                    -- net sales is 0 because the return (RT / RTO) is processed, but the settlement does not match
                    WHEN abs(COALESCE(s.net_sales, 0)) <= %s
                         AND (COALESCE(s.rt_sales, 0) <> 0 OR COALESCE(s.rto_sales, 0) <> 0)
                                                    THEN 'RETURN_PROCESSED_SETTLEMENT_NOT_ADJUSTED'
                    WHEN COALESCE(s.net_sales, 0) > COALESCE(t.net_settlement, 0) THEN 'SALES_HIGHER_THAN_SETTLEMENT'
                    ELSE 'SETTLEMENT_HIGHER_THAN_SALES' END AS status
        FROM net_sales s
        FULL OUTER JOIN net_settlement t ON s.packet_id = t.order_release_id
    """, (run_id, tolerance, tolerance))


INS = """INSERT INTO recon_exceptions
    (run_id, category, exception_type, dataset, row_no, packet_id, order_release_id, amount_1, amount_2, difference, description) """


def build_recon_exceptions(cur, run_id, tolerance, commission_tol, platform, m):
    log = hygiene.CheckLog(cur, run_id, "RECONCILIATION", EXCEPTIONS)
    cur.execute("SELECT count(*) AS n FROM recon_results WHERE run_id = %s", (run_id,))
    n_results = cur.fetchone()["n"]

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

    # Re-performed tax on forward settlement (commission variance removed)
    fp = sql.SQL("f.{}").format(fpref) if fpref else sql.SQL("NULL")
    names = {r: m.name("settlement_forward", r) for r in ("amount", "taxable_amount", "tax_rate", "igst", "cgst", "sgst")}
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


def _n(alias, col):
    """numeric value of a column, 0 when blank / not a number"""
    return sql.SQL("COALESCE(try_numeric({}.{}), 0)").format(sql.SQL(alias), col)


def _fee_sum(alias, m, dataset):
    """shipping + fixed + pick and pack + payment gateway fee (absolute values), None when none is mapped."""
    cols = [m.col(dataset, r) for r in ("shipping_fee", "fixed_fee", "pick_pack_fee", "pg_fee")]
    cols = [c for c in cols if c]
    if not cols:
        return None
    return sql.SQL("(") + sql.SQL(" + ").join(sql.SQL("abs({})").format(_n(alias, c)) for c in cols) + sql.SQL(")")


DEFAULT_RATES = {"gst": config.LOGISTICS_GST_RATE, "tcs": config.TCS_RATE, "tds": config.TDS_RATE,
                 "pg": config.PG_FEE_RATE}


def _formula_checks(cur, log, run_id, tol, rates, m, fp, fk, f_filter, f_params):
    """The confirmed settlement-file formulas, re-performed per forward row. Each line: actual vs expected; a row
    is flagged when they differ by more than the tolerance. Amounts compared as absolute values (deductions are
    stored as negatives). A formula whose columns are not mapped is skipped."""
    F = "settlement_forward"
    c = {r: m.col(F, r) for r in (
        "customer_paid", "prepaid_amount", "postpaid_amount", "taxable_amount", "tax_rate", "commission_pct_col",
        "platform_fees", "commission", "prepaid_commission", "postpaid_commission", "tcs", "tds", "tcs_prepaid",
        "tcs_postpaid", "tds_prepaid", "tds_postpaid", "commission_tcs_tds", "pg_fee", "logistics",
        "prepaid_comm_deduction", "postpaid_comm_deduction", "prepaid_log_deduction", "postpaid_log_deduction",
        "expected_settlement")}
    if not c["customer_paid"]:
        c["customer_paid"] = m.col(F, "amount")
    n = {r: (_n("f", col) if col else None) for r, col in c.items()}
    A = lambda r: sql.SQL("abs({})").format(n[r])                                        # noqa: E731
    lit = sql.Literal
    share = lambda part: sql.SQL("({} / nullif({}, 0))").format(A(part), A("customer_paid"))  # noqa: E731

    # code -> [(what, needed roles, actual, expected)]
    checks = {
        "AMOUNT_SPLIT_MISMATCH": [
            ("prepaid_amount + postpaid_amount vs customer paid", ("prepaid_amount", "postpaid_amount", "customer_paid"),
             lambda: sql.SQL("{} + {}").format(A("prepaid_amount"), A("postpaid_amount")), lambda: A("customer_paid"))],
        "TAXABLE_AMOUNT_VARIANCE": [
            ("taxable_amount vs customer paid / (1 + tax rate)", ("taxable_amount", "tax_rate", "customer_paid"),
             lambda: A("taxable_amount"),
             lambda: sql.SQL("{} / (1 + {} / 100.0)").format(A("customer_paid"), n["tax_rate"]))],
        "COMMISSION_CALC_VARIANCE": [
            ("platform_fees vs customer paid x commission_percentage", ("platform_fees", "commission_pct_col", "customer_paid"),
             lambda: A("platform_fees"), lambda: sql.SQL("{} * {} / 100.0").format(A("customer_paid"), n["commission_pct_col"])),
            ("total_commission vs -platform_fees", ("commission", "platform_fees"),
             lambda: A("commission"), lambda: A("platform_fees"))],
        "COMMISSION_SPLIT_MISMATCH": [
            ("prepaid + postpaid total commission vs total_commission", ("prepaid_commission", "postpaid_commission", "commission"),
             lambda: sql.SQL("{} + {}").format(A("prepaid_commission"), A("postpaid_commission")), lambda: A("commission"))],
        "TCS_TDS_VARIANCE": [
            (f"tcs_amount vs taxable x {rates['tcs']:g}%", ("tcs", "taxable_amount"),
             lambda: A("tcs"), lambda: sql.SQL("{} * {} / 100.0").format(A("taxable_amount"), lit(rates["tcs"]))),
            (f"tds_amount vs taxable x {rates['tds']:g}%", ("tds", "taxable_amount"),
             lambda: A("tds"), lambda: sql.SQL("{} * {} / 100.0").format(A("taxable_amount"), lit(rates["tds"]))),
            ("commission+TCS+TDS deduction vs commission + TCS + TDS", ("commission_tcs_tds", "commission", "tcs", "tds"),
             lambda: A("commission_tcs_tds"),
             lambda: sql.SQL("{} + {} + {}").format(A("commission"), A("tcs"), A("tds")))],
        "TCS_TDS_SPLIT_MISMATCH": [
            (f"{col} vs {part} share of {tot}", (col, part, tot, "customer_paid"),
             (lambda col=col: A(col)), (lambda part=part, tot=tot: sql.SQL("{} * {}").format(share(part), A(tot))))
            for col, part, tot in (("tcs_prepaid", "prepaid_amount", "tcs"), ("tcs_postpaid", "postpaid_amount", "tcs"),
                                   ("tds_prepaid", "prepaid_amount", "tds"), ("tds_postpaid", "postpaid_amount", "tds"))],
        "PG_FEE_VARIANCE": [
            (f"payment_gateway_fee vs customer paid x {rates['pg']:g}%", ("pg_fee", "customer_paid"),
             lambda: A("pg_fee"), lambda: sql.SQL("{} * {} / 100.0").format(A("customer_paid"), lit(rates["pg"])))],
        "DEDUCTION_SPLIT_MISMATCH": [
            (f"{col} vs {part} share of {tot}", (col, part, tot, "customer_paid"),
             (lambda col=col: A(col)), (lambda part=part, tot=tot: sql.SQL("{} * {}").format(share(part), A(tot))))
            for col, part, tot in (
                ("prepaid_comm_deduction", "prepaid_amount", "commission_tcs_tds"),
                ("postpaid_comm_deduction", "postpaid_amount", "commission_tcs_tds"),
                ("prepaid_log_deduction", "prepaid_amount", "logistics"),
                ("postpaid_log_deduction", "postpaid_amount", "logistics"))],
        "EXPECTED_SETTLEMENT_VARIANCE": [
            ("total_expected_settlement vs customer paid - commission/TCS/TDS - logistics",
             ("expected_settlement", "customer_paid", "commission_tcs_tds", "logistics"),
             lambda: n["expected_settlement"],
             lambda: sql.SQL("{} - {} - {}").format(A("customer_paid"), A("commission_tcs_tds"), A("logistics")))],
    }
    for code, lines in checks.items():
        ran, missing = [], set()
        for what, need, actual, expected in lines:
            gone = [r for r in need if not c[r]]
            if gone:
                missing.update(gone)
                continue
            a, e = sql.SQL("({})").format(actual()), sql.SQL("({})").format(expected())
            cur.execute(sql.SQL(INS + """
                SELECT %s, 'RECONCILIATION', %s, 'settlement_forward', f."_row_no", {fp}, btrim(f.{fk}),
                       round({a}, 2), round({e}, 2), round(abs({a} - {e}), 2),
                       %s || ': ' || round({a}, 2) || ' vs expected ' || round({e}, 2)
                FROM settlement_forward f WHERE abs({a} - {e}) > %s {pf}""").format(
                fp=fp, fk=fk, a=a, e=e, pf=f_filter), [run_id, code, what, tol] + f_params)
            ran.append(what)
        names = sorted({m.name(F, r) or r for _, need, _, _ in lines for r in need if c[r]})
        if ran:
            log.add(code, F, columns=names, note=("Not checked (column not mapped): "
                                                  + ", ".join(sorted(missing))) if missing else None)
        else:
            log.add(code, F, skipped="Columns not mapped: " + ", ".join(sorted(missing)))


def build_fee_checks(cur, run_id, commission_pct, logistics_pct, tol, platform, m, rates=None):
    """Commission %, logistics %, logistics components / tax, reverse settlement rules, actual settlement,
    pending settlement, claim amount, payments and bank UTRs.

    Commission % = |total commission| / net sales (customer paid) x 100, per forward settlement row
                   (commission is never on reverse). The base column is chosen on the Run screen.
    Logistics %  = (|forward logistics| + |reverse logistics|) / forward MRP x 100, per order. The reverse rows are
                   read from every uploaded month, so an order returned after the run month still gets its reverse
                   logistics. Amounts are taken as absolute values (the file stores deductions as negatives).
    """
    log = hygiene.CheckLog(cur, run_id, "RECONCILIATION", EXCEPTIONS)
    rates = {**DEFAULT_RATES, **{k: v for k, v in (rates or {}).items() if v is not None}}
    gst_rate = rates["gst"]
    F, R = "settlement_forward", "settlement_reverse"
    fk, rk = m.col(F, "key"), m.col(R, "key")
    fpref = m.col(F, "packet_ref")
    fp = sql.SQL("f.{}").format(fpref) if fpref else sql.SQL("NULL")
    f_filter, f_params = _platform_filter("f", platform, m.col(F, "platform"))
    mrp, comm, logi = m.col(F, "mrp"), m.col(F, "commission"), m.col(F, "logistics")
    cbase, cbase_name = m.col(F, "commission_base"), m.name(F, "commission_base")
    rcomm, rlog = m.col(R, "commission"), m.col(R, "logistics")

    # ---- commission above the allowed % of net sales (forward rows)
    if commission_pct is None:
        log.add("COMMISSION_ABOVE_ALLOWED", F, skipped="No allowed commission % entered on the Run screen")
    elif not (cbase and comm):
        log.add("COMMISSION_ABOVE_ALLOWED", F, skipped="No 'Commission % base (net sales)' / 'Total commission' column mapped")
    else:
        cur.execute(sql.SQL(INS + """
            SELECT %s, 'RECONCILIATION', 'COMMISSION_ABOVE_ALLOWED', 'settlement_forward', f."_row_no", {fp}, btrim(f.{fk}),
                   round(abs({c}), 2), round({b} * {p} / 100.0, 2), round(abs({c}) - {b} * {p} / 100.0, 2),
                   'Commission ' || round(abs({c}), 2) || ' = ' || round(abs({c}) / {b} * 100, 2) || '%% of net sales '
                     || round({b}, 2) || ', allowed ' || {p} || '%% = ' || round({b} * {p} / 100.0, 2)
            FROM settlement_forward f
            WHERE {b} > 0 AND round(abs({c}) / {b} * 100, 2) > {p} {pf}""").format(
            fp=fp, fk=fk, c=_n("f", comm), b=_n("f", cbase), p=sql.Literal(commission_pct), pf=f_filter),
            [run_id] + f_params)
        log.add("COMMISSION_ABOVE_ALLOWED", F, columns=[m.name(F, "commission"), cbase_name],
                note=f"Allowed {commission_pct:g}% of net sales ({cbase_name})")

    # ---- base missing where a fee % is needed: net sales for commission, MRP for logistics
    parts = [(comm, cbase, "commission %% (net sales"), (logi, mrp, "logistics %% (MRP")]
    parts = [(fee, base, what) for fee, base, what in parts if fee and base]
    if parts:
        cond = sql.SQL(" OR ").join(sql.SQL("({} <> 0 AND {} <= 0)").format(_n("f", fee), _n("f", base))
                                    for fee, base, _ in parts)
        why = sql.SQL(" || ").join(sql.SQL(
            "CASE WHEN {fee} <> 0 AND {bn} <= 0 THEN {what} || ' is ' || coalesce(nullif(btrim(f.{b}), ''), '<blank>') || '); ' ELSE '' END"
        ).format(fee=_n("f", fee), bn=_n("f", base), b=base, what=sql.Literal(what + " base")) for fee, base, what in parts)
        cur.execute(sql.SQL(INS + """
            SELECT %s, 'RECONCILIATION', 'FEE_BASE_MISSING', 'settlement_forward', f."_row_no", {fp}, btrim(f.{fk}),
                   NULL, NULL, NULL, 'Cannot calculate: ' || rtrim({why}, '; ')
            FROM settlement_forward f WHERE ({cond}) {pf}""").format(
            fp=fp, fk=fk, why=why, cond=cond, pf=f_filter), [run_id] + f_params)
        log.add("FEE_BASE_MISSING", F, columns=[cbase_name, m.name(F, "mrp")])

    # ---- logistics (forward + reverse of the order) above the allowed % of MRP
    if logistics_pct is None:
        log.add("LOGISTICS_ABOVE_ALLOWED", F, skipped="No allowed logistics % entered on the Run screen")
    elif not (mrp and logi):
        log.add("LOGISTICS_ABOVE_ALLOWED", F, skipped="No 'MRP' / 'Total logistics deduction' column mapped")
    else:
        r_filter, r_params = _platform_filter("r", platform, m.col(R, "platform"))
        rev = (sql.SQL("""SELECT btrim(r.{rk}) AS k, sum(abs({rl})) AS rl FROM public.settlement_reverse r
                          WHERE btrim(coalesce(r.{rk}, '')) <> '' {rf} GROUP BY 1""").format(
            rk=rk, rl=_n("r", rlog), rf=r_filter) if rlog else sql.SQL("SELECT NULL::text AS k, 0::numeric AS rl"))
        cur.execute(sql.SQL(INS + """
            WITH fw AS (SELECT btrim(f.{fk}) AS k, max({fp}) AS pref, sum(abs({fl})) AS fl, sum({mrp}) AS mrp
                        FROM settlement_forward f WHERE btrim(coalesce(f.{fk}, '')) <> '' {pf} GROUP BY 1),
                 rv AS ({rev}),
                 t  AS (SELECT fw.*, coalesce(rv.rl, 0) AS rl, fw.fl + coalesce(rv.rl, 0) AS total
                        FROM fw LEFT JOIN rv ON rv.k = fw.k)
            SELECT %s, 'RECONCILIATION', 'LOGISTICS_ABOVE_ALLOWED', 'settlement_forward', NULL, pref, k,
                   round(total, 2), round(mrp * {p} / 100.0, 2), round(total - mrp * {p} / 100.0, 2),
                   'Logistics ' || round(total, 2) || ' (forward ' || round(fl, 2) || ' + reverse ' || round(rl, 2)
                     || ') = ' || round(total / mrp * 100, 2) || '%% of MRP ' || round(mrp, 2)
                     || ', allowed ' || {p} || '%% = ' || round(mrp * {p} / 100.0, 2)
            FROM t WHERE mrp > 0 AND round(total / mrp * 100, 2) > {p}""").format(
            fk=fk, fp=fp, fl=_n("f", logi), mrp=_n("f", mrp), pf=f_filter, rev=rev, p=sql.Literal(logistics_pct)),
            f_params + r_params + [run_id])
        log.add("LOGISTICS_ABOVE_ALLOWED", F,
                columns=[m.name(F, "logistics"), m.name(F, "mrp"),
                         f"Settlement - Reverse.{m.name(R, 'logistics')}" if rlog else None],
                note=f"Allowed {logistics_pct:g}% of MRP" + ("" if rlog else
                     "; reverse logistics not mapped, forward logistics only"))

    # ---- per row, forward and reverse: components add up to the total, tax = GST rate x fees
    for ds, alias in ((F, "f"), (R, "r")):
        key, pref = m.col(ds, "key"), m.col(ds, "packet_ref")
        pr = sql.SQL("{}.{}").format(sql.SQL(alias), pref) if pref else sql.SQL("NULL")
        pf, pp = _platform_filter(alias, platform, m.col(ds, "platform"))
        total, tax, fees = m.col(ds, "logistics"), m.col(ds, "logistics_tax"), _fee_sum(alias, m, ds)
        fmt = dict(t=db.q(ds), a=sql.SQL(alias), pr=pr, k=key, pf=pf)
        if total and fees:
            parts = fees if not tax else sql.SQL("({} + abs({}))").format(fees, _n(alias, tax))
            cur.execute(sql.SQL(INS + """
                SELECT %s, 'RECONCILIATION', 'LOGISTICS_COMPONENT_MISMATCH', %s, {a}."_row_no", {pr}, btrim({a}.{k}),
                       round(abs({tot}), 2), round({parts}, 2), round(abs(abs({tot}) - {parts}), 2),
                       'Total logistics ' || round(abs({tot}), 2) || ' vs fees + tax ' || round({parts}, 2)
                FROM {t} {a} WHERE abs(abs({tot}) - {parts}) > %s {pf}""").format(
                tot=_n(alias, total), parts=parts, **fmt), [run_id, ds, tol] + pp)
            log.add("LOGISTICS_COMPONENT_MISMATCH", ds, columns=[m.name(ds, r) for r in (
                "logistics", "shipping_fee", "fixed_fee", "pick_pack_fee", "pg_fee", "logistics_tax")])
        else:
            log.add("LOGISTICS_COMPONENT_MISMATCH", ds, skipped="No 'Total logistics deduction' / fee columns mapped")
        if tax and fees:
            rate = gst_rate
            cur.execute(sql.SQL(INS + """
                SELECT %s, 'RECONCILIATION', 'LOGISTICS_TAX_VARIANCE', %s, {a}."_row_no", {pr}, btrim({a}.{k}),
                       round(abs({tax}), 2), round({fees} * {rate} / 100.0, 2),
                       round(abs(abs({tax}) - {fees} * {rate} / 100.0), 2),
                       'Tax on logistics ' || round(abs({tax}), 2) || ' vs ' || {rate} || '%% x fees ' || round({fees}, 2)
                         || ' = ' || round({fees} * {rate} / 100.0, 2)
                FROM {t} {a} WHERE abs(abs({tax}) - {fees} * {rate} / 100.0) > %s {pf}""").format(
                tax=_n(alias, tax), fees=fees, rate=sql.Literal(rate), **fmt), [run_id, ds, tol] + pp)
            log.add("LOGISTICS_TAX_VARIANCE", ds, note=f"GST rate {rate:g}%", columns=[m.name(ds, r) for r in (
                "logistics_tax", "shipping_fee", "fixed_fee", "pick_pack_fee", "pg_fee")])
        else:
            log.add("LOGISTICS_TAX_VARIANCE", ds, skipped="No 'Tax on logistics' / fee columns mapped")

    # ---- reverse settlement rules: logistics always present, commission never
    rpref = m.col(R, "packet_ref")
    rp = sql.SQL("r.{}").format(rpref) if rpref else sql.SQL("NULL")
    r_filter, r_params = _platform_filter("r", platform, m.col(R, "platform"))
    for code, col, role, cond, text in (
            ("REVERSE_LOGISTICS_MISSING", rlog, "logistics", "{v} = 0",
             "'Logistics deduction is ' || coalesce(nullif(btrim(r.{c}), ''), '<blank>')"),
            ("REVERSE_HAS_COMMISSION", rcomm, "commission", "{v} <> 0",
             "'Commission ' || round({v}, 2) || ' on a reverse settlement'")):
        if not col:
            log.add(code, R, skipped=f"No '{colmap.field(R, role)['label']}' column mapped on Settlement - Reverse")
            continue
        v = _n("r", col)
        cur.execute(sql.SQL(INS + """
            SELECT %s, 'RECONCILIATION', %s, 'settlement_reverse', r."_row_no", {rp}, btrim(r.{rk}),
                   {v}, NULL, NULL, {text}
            FROM settlement_reverse r WHERE """ + cond + " {rf}").format(
            rp=rp, rk=rk, v=v, text=sql.SQL(text).format(v=v, c=col), rf=r_filter), [run_id, code] + r_params)
        log.add(code, R, columns=m.name(R, role))

    # ---- total actual settlement vs customer paid - (commission + TCS + TDS) - logistics (forward rows)
    paid = m.col(F, "customer_paid") or m.col(F, "amount")
    paid_name = m.name(F, "customer_paid") or m.name(F, "amount")
    ctt, actual = m.col(F, "commission_tcs_tds"), m.col(F, "actual_settlement")
    if not (paid and ctt and logi and actual):
        why = ("Needs 'Customer paid', 'Commission + TCS + TDS deduction', 'Total logistics deduction' and "
               "'Total actual settlement' columns mapped")
        for code in ("SETTLEMENT_SHORT_PAID", "SETTLEMENT_EXCESS_PAID", "PENDING_SETTLEMENT_MISMATCH",
                     "CLAIM_RECOVERABLE"):
            log.add(code, F, skipped=why)
    else:
        P, C, L = _n("f", paid), _n("f", ctt), _n("f", logi)
        exp = sql.SQL("({p} - abs({c}) - abs({l}))").format(p=P, c=C, l=L)
        act = sql.SQL("try_numeric(f.{})").format(actual)
        cols = [m.name(F, "actual_settlement"), paid_name, m.name(F, "commission_tcs_tds"), m.name(F, "logistics")]
        for code, cond, diff in (("SETTLEMENT_SHORT_PAID", "{exp} - {act} > %s", "{exp} - {act}"),
                                 ("SETTLEMENT_EXCESS_PAID", "{act} - {exp} > %s", "{act} - {exp}")):
            cur.execute(sql.SQL(INS + """
                SELECT %s, 'RECONCILIATION', %s, 'settlement_forward', f."_row_no", {fp}, btrim(f.{fk}),
                       round({exp}, 2), round({act}, 2), round(""" + diff + """, 2),
                       'Expected ' || round({exp}, 2) || ' (customer paid ' || round({paid}, 2) || ' - commission+TCS+TDS '
                         || round(abs({ctt}), 2) || ' - logistics ' || round(abs({logi}), 2) || ') vs actual ' || round({act}, 2)
                FROM settlement_forward f WHERE {act} IS NOT NULL AND """ + cond + " {pf}").format(
                fp=fp, fk=fk, exp=exp, act=act, paid=P, ctt=C, logi=L, pf=f_filter),
                [run_id, code, tol] + f_params)
            log.add(code, F, columns=cols)

        # pending = expected - actual settlement (signed: negative = paid in excess).
        # Short paid with nothing marked pending = money missing
        pend = m.col(F, "pending_settlement")
        if pend:
            owed = sql.SQL("({exp} - {act})").format(exp=exp, act=act)
            pn = _n("f", pend)
            cur.execute(sql.SQL(INS + """
                SELECT %s, 'RECONCILIATION', 'PENDING_SETTLEMENT_MISMATCH', 'settlement_forward', f."_row_no", {fp},
                       btrim(f.{fk}), round({owed}, 2), round({pn}, 2), round(abs({owed} - {pn}), 2),
                       CASE WHEN {owed} > %s AND abs({pn}) <= %s
                            THEN 'Short paid ' || round({owed}, 2) || ' but nothing marked pending: money missing'
                            ELSE 'Expected ' || round({exp}, 2) || ' - actual ' || round({act}, 2) || ' = '
                                 || round({owed}, 2) || ' but pending settlement says ' || round({pn}, 2) END
                FROM settlement_forward f
                WHERE {act} IS NOT NULL AND abs({owed} - {pn}) > %s {pf}""").format(
                fp=fp, fk=fk, owed=owed, pn=pn, exp=exp, act=act, pf=f_filter), [run_id, tol, tol, tol] + f_params)
            log.add("PENDING_SETTLEMENT_MISMATCH", F, columns=cols + [m.name(F, "pending_settlement")])
        else:
            log.add("PENDING_SETTLEMENT_MISMATCH", F, skipped="No 'Amount pending settlement' column mapped")

        # claim: fair settlement (commission capped at allowed % of net sales, logistics at allowed % of MRP) - actual
        if commission_pct is None and logistics_pct is None:
            log.add("CLAIM_RECOVERABLE", F, skipped="Enter an allowed commission % and / or logistics %")
        else:
            ca = _n("f", comm) if comm else sql.SQL("0")

            def capped(fee, base, pct):
                if pct is None or base is None:
                    return sql.SQL("abs({})").format(fee)
                return sql.SQL("CASE WHEN {b} > 0 THEN least(abs({f}), {b} * {p} / 100.0) ELSE abs({f}) END").format(
                    b=_n("f", base), f=fee, p=sql.Literal(pct))

            fair_c, fair_l = capped(ca, cbase, commission_pct), capped(L, mrp, logistics_pct)
            tcs_tds = sql.SQL("greatest(abs({c}) - abs({a}), 0)").format(c=C, a=ca)
            fair = sql.SQL("({p} - {fc} - {tt} - {fl})").format(p=P, fc=fair_c, tt=tcs_tds, fl=fair_l)
            cur.execute(sql.SQL(INS + """
                SELECT %s, 'RECONCILIATION', 'CLAIM_RECOVERABLE', 'settlement_forward', f."_row_no", {fp}, btrim(f.{fk}),
                       round({fair}, 2), round({act}, 2), round({fair} - {act}, 2),
                       'Fair settlement ' || round({fair}, 2) || ' = customer paid ' || round({p}, 2)
                         || ' - commission ' || round({fc}, 2) || ' (charged ' || round(abs({ca}), 2) || ')'
                         || ' - TCS+TDS ' || round({tt}, 2)
                         || ' - logistics ' || round({fl}, 2) || ' (charged ' || round(abs({l}), 2) || ')'
                         || '; actual ' || round({act}, 2) || ' -> claim ' || round({fair} - {act}, 2)
                FROM settlement_forward f WHERE {act} IS NOT NULL AND {fair} - {act} > %s {pf}""").format(
                fp=fp, fk=fk, fair=fair, act=act, p=P, fc=fair_c, ca=ca, tt=tcs_tds, fl=fair_l, l=L, pf=f_filter),
                [run_id, tol] + f_params)
            log.add("CLAIM_RECOVERABLE", F, columns=cols + [m.name(F, "commission"), cbase_name, m.name(F, "mrp")],
                    note="Commission capped at " + (f"{commission_pct:g}% of net sales" if commission_pct is not None
                                                    else "the amount charged")
                         + ", logistics at " + (f"{logistics_pct:g}% of MRP" if logistics_pct is not None
                                                else "the amount charged"))

    _formula_checks(cur, log, run_id, tol, rates, m, fp, fk, f_filter, f_params)

    # ---- payments: prepaid + postpaid = total actual settlement; a payment needs a bank UTR
    pre, post = m.col(F, "prepaid_payment"), m.col(F, "postpaid_payment")
    if pre and post and actual:
        pay = sql.SQL("({} + {})").format(_n("f", pre), _n("f", post))
        cur.execute(sql.SQL(INS + """
            SELECT %s, 'RECONCILIATION', 'PAYMENT_SPLIT_MISMATCH', 'settlement_forward', f."_row_no", {fp}, btrim(f.{fk}),
                   round({pay}, 2), round({act}, 2), round(abs({pay} - {act}), 2),
                   'Prepaid ' || round({pre}, 2) || ' + postpaid ' || round({post}, 2) || ' = ' || round({pay}, 2)
                     || ' vs total actual settlement ' || round({act}, 2)
            FROM settlement_forward f
            WHERE try_numeric(f.{actual}) IS NOT NULL AND abs({pay} - {act}) > %s {pf}""").format(
            fp=fp, fk=fk, pay=pay, act=_n("f", actual), actual=actual, pre=_n("f", pre), post=_n("f", post),
            pf=f_filter), [run_id, tol] + f_params)
        log.add("PAYMENT_SPLIT_MISMATCH", F, columns=[m.name(F, r) for r in
                                                      ("prepaid_payment", "postpaid_payment", "actual_settlement")])
    else:
        log.add("PAYMENT_SPLIT_MISMATCH", F,
                skipped="Needs 'Prepaid payment', 'Postpaid payment' and 'Total actual settlement' columns mapped")

    utr_parts = []
    for kind, amt, utr, dt in (("Prepaid", pre, m.col(F, "utr_prepaid"), m.col(F, "date_prepaid")),
                               ("Postpaid", post, m.col(F, "utr_postpaid"), m.col(F, "date_postpaid"))):
        if not (amt and utr):
            continue
        cur.execute(sql.SQL(INS + """
            SELECT %s, 'RECONCILIATION', 'PAYMENT_WITHOUT_UTR', 'settlement_forward', f."_row_no", {fp}, btrim(f.{fk}),
                   round({a}, 2), NULL, round(abs({a}), 2), %s || ' payment ' || round({a}, 2) || ' has no bank UTR'
            FROM settlement_forward f WHERE {a} <> 0 AND btrim(coalesce(f.{u}, '')) IN ('', '0') {pf}""").format(
            fp=fp, fk=fk, a=_n("f", amt), u=utr, pf=f_filter), [run_id, kind] + f_params)
        utr_parts.append(sql.SQL("""SELECT {kind} AS kind, btrim(f.{u}) AS utr, {d} AS d, btrim(f.{fk}) AS k, {a} AS a
                                    FROM settlement_forward f
                                    WHERE {a} <> 0 AND btrim(coalesce(f.{u}, '')) NOT IN ('', '0') {pf}""").format(
            kind=sql.Literal(kind), u=utr, d=sql.SQL("try_date(f.{})").format(dt) if dt else sql.SQL("NULL::date"),
            fk=fk, a=_n("f", amt), pf=f_filter))
    if utr_parts:
        log.add("PAYMENT_WITHOUT_UTR", F, columns=[m.name(F, r) for r in
                                                   ("prepaid_payment", "utr_prepaid", "postpaid_payment", "utr_postpaid")])
        # payments per UTR = what the bank statement should show as one credit each (bank match once it is uploaded)
        cur.execute(sql.SQL("""
            INSERT INTO recon_utr (run_id, payment_type, utr, settlement_date, orders, rows, amount)
            SELECT %s, kind, utr, max(d), count(DISTINCT k), count(*), round(sum(a), 2)
            FROM ({}) x GROUP BY kind, utr""").format(sql.SQL(" UNION ALL ").join(utr_parts)),
            [run_id] + f_params * len(utr_parts))
    else:
        log.add("PAYMENT_WITHOUT_UTR", F, skipped="No 'Prepaid / Postpaid payment' + 'Bank UTR' columns mapped")


def attach_to_results(cur, run_id):
    """Put the exceptions into the result: every result row gets the reconciliation exceptions and the hygiene
    flags of its key, and a reverse settlement with no forward settlement and no sales becomes a result row of
    its own (status REVERSE_SETTLEMENT_WITHOUT_FORWARD), so nothing is only visible on a separate page."""
    # orphan reverse settlements -> own result row (amounts kept out of net settlement / difference totals)
    cur.execute("""
        INSERT INTO recon_results (run_id, packet_id, order_release_id, reverse_settlement, status)
        SELECT %s, NULL, e.k, e.amt, 'REVERSE_SETTLEMENT_WITHOUT_FORWARD'
        FROM (SELECT btrim(order_release_id) AS k, sum(amount_2) AS amt FROM recon_exceptions
              WHERE run_id = %s AND exception_type = 'REVERSE_SETTLEMENT_WITHOUT_FORWARD'
                AND btrim(coalesce(order_release_id, '')) <> ''
              GROUP BY 1) e
        WHERE NOT EXISTS (SELECT 1 FROM recon_results r
                          WHERE r.run_id = %s AND coalesce(r.order_release_id, r.packet_id) = e.k)""",
                (run_id, run_id, run_id))
    cur.execute("""
        UPDATE recon_results r SET exceptions = e.t
        FROM (SELECT btrim(coalesce(order_release_id, packet_id)) AS k, array_agg(DISTINCT exception_type) AS t
              FROM recon_exceptions WHERE run_id = %s AND category = 'RECONCILIATION'
                AND coalesce(order_release_id, packet_id) IS NOT NULL
              GROUP BY 1) e
        WHERE r.run_id = %s AND coalesce(r.order_release_id, r.packet_id) = e.k""", (run_id, run_id))
    # the status already says it - do not repeat it as an exception of the same row
    cur.execute("""UPDATE recon_results SET exceptions = NULLIF(array_remove(exceptions, status), '{}')
                   WHERE run_id = %s AND status = 'REVERSE_SETTLEMENT_WITHOUT_FORWARD'""", (run_id,))
    cur.execute("""
        UPDATE recon_results r SET hygiene_flags = h.t
        FROM (SELECT k, array_agg(DISTINCT t) AS t FROM (
                SELECT btrim(packet_id) AS k, exception_type AS t FROM recon_exceptions
                WHERE run_id = %s AND category = 'HYGIENE' AND dataset LIKE 'sales%%' AND packet_id IS NOT NULL
                UNION ALL
                SELECT btrim(order_release_id), exception_type FROM recon_exceptions
                WHERE run_id = %s AND category = 'HYGIENE' AND dataset LIKE 'settlement%%' AND order_release_id IS NOT NULL
              ) x GROUP BY k) h
        WHERE r.run_id = %s AND coalesce(r.packet_id, r.order_release_id) = h.k""", (run_id, run_id, run_id))


def run_reconciliation(recon_month: date, tolerance: float, return_window: int, commission_tol: float, platform: str,
                       mapping=None, recon_month_to: date = None, commission_pct: float = None,
                       logistics_pct: float = None, rates: dict = None):
    """Full pipeline. Returns run_id.

    recon_month .. recon_month_to: the month(s) reconciled together (recon_month_to None = one month).
    commission_pct / logistics_pct: allowed % of net sales / MRP for the fee checks (None = that check is not run).
    rates: {"gst", "tcs", "tds", "pg"} in % for the formula checks (missing = config default).

    mapping: mapping.Mapping chosen on the Run screen. None = the standard column names.
    """
    recon_month_to = recon_month_to or recon_month
    r = {**DEFAULT_RATES, **{k: v for k, v in (rates or {}).items() if v is not None}}
    with db.cursor() as cur:
        m = mapping or colmap.default_mapping(cur)
        cur.execute("""INSERT INTO recon_runs (recon_month, recon_month_to, tolerance, return_window, commission_tol,
                                               platform_filter, column_mapping, commission_pct, logistics_pct,
                                               logistics_gst_rate, tcs_rate, tds_rate, pg_rate)
                       VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) RETURNING run_id""",
                    (recon_month, recon_month_to, tolerance, return_window, commission_tol, platform or None, m.to_json(),
                     commission_pct, logistics_pct, *(r[k] for k in ("gst", "tcs", "tds", "pg"))))
        run_id = cur.fetchone()["run_id"]
        try:
            # fail with a clear error instead of hanging when another session (pgAdmin, an old run) holds a lock
            cur.execute("SET lock_timeout = '30s'")
            _step("normalise ids", colmap.normalise_ids, cur, m)
            _step("index keys", _index_keys, cur, m)
            # only this month's keys + open items carried from earlier months are reconciled (see carry.py)
            scope = carry.build_scope(cur, recon_month, platform, m, recon_month_to)
            log.info("Scope: %s keys (%s carried from earlier months)", scope["n"], scope["c"])
            _step("scoped tables", carry.shadow_tables, cur, recon_month_to, m)
            t = time.time()
            log.info("Run step 'hygiene' started")
            hyg = hygiene.run_hygiene(cur, run_id, recon_month, return_window, m, recon_month_to)
            log.info("Run step 'hygiene' done in %.1fs", time.time() - t)
            _step("net sales", build_net_sales, cur, m)
            _step("net settlement", build_net_settlement, cur, platform, m)
            _step("results", build_results, cur, run_id, tolerance)
            _step("recon exceptions", build_recon_exceptions, cur, run_id, tolerance, commission_tol, platform, m)
            _step("fee checks", build_fee_checks, cur, run_id, commission_pct, logistics_pct, commission_tol, platform, m,
                  r)
            _step("exceptions into results", attach_to_results, cur, run_id)
            cf = carry.update_open_items(cur, run_id, recon_month_to, platform, m)
            log.info("Carry-forward: %s", cf)

            cur.execute("""
                UPDATE recon_runs SET
                    status = 'DONE',
                    total_sales      = (SELECT count(*) FROM net_sales),
                    total_settlement = (SELECT count(*) FROM net_settlement),
                    matched          = (SELECT count(*) FROM recon_results WHERE run_id=%s AND status='MATCHED'),
                    unmatched        = (SELECT count(*) FROM recon_results WHERE run_id=%s
                                          AND status IN ('SALES_HIGHER_THAN_SETTLEMENT', 'SETTLEMENT_HIGHER_THAN_SALES',
                                                         'RETURN_PROCESSED_SETTLEMENT_NOT_ADJUSTED')),
                    return_not_adjusted = (SELECT count(*) FROM recon_results WHERE run_id=%s
                                             AND status='RETURN_PROCESSED_SETTLEMENT_NOT_ADJUSTED'),
                    sales_higher     = (SELECT count(*) FROM recon_results WHERE run_id=%s AND status='SALES_HIGHER_THAN_SETTLEMENT'),
                    settlement_higher= (SELECT count(*) FROM recon_results WHERE run_id=%s AND status='SETTLEMENT_HIGHER_THAN_SALES'),
                    reverse_only     = (SELECT count(*) FROM recon_results WHERE run_id=%s AND status='REVERSE_SETTLEMENT_WITHOUT_FORWARD'),
                    sales_only       = (SELECT count(*) FROM recon_results WHERE run_id=%s AND status='SALES_NOT_IN_SETTLEMENT'),
                    settlement_only  = (SELECT count(*) FROM recon_results WHERE run_id=%s AND status='SETTLEMENT_NOT_IN_SALES'),
                    exception_count  = (SELECT count(*) FROM recon_exceptions WHERE run_id=%s AND category='RECONCILIATION'),
                    hygiene_count    = %s,
                    carried_in = %s, closed_prior = %s, new_open = %s, open_total = %s, awaiting_settlement = %s
                WHERE run_id = %s""", (run_id, run_id, run_id, run_id, run_id, run_id, run_id, run_id, run_id, hyg,
                                       cf["carried_in"], cf["closed_prior"], cf["new_open"], cf["open_total"],
                                       cf["awaiting"], run_id))
        except Exception as e:
            cur.connection.rollback()   # discards partial results of this run
            with db.cursor() as c2:
                c2.execute("""INSERT INTO recon_runs (recon_month, recon_month_to, tolerance, return_window, commission_tol,
                                                      platform_filter, status, message, commission_pct, logistics_pct)
                              VALUES (%s,%s,%s,%s,%s,%s,'FAILED',%s,%s,%s)""",
                           (recon_month, recon_month_to, tolerance, return_window, commission_tol, platform or None,
                            str(e)[:2000], commission_pct, logistics_pct))
            raise
    return run_id
