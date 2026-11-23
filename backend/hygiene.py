"""Hygiene checks (BRD section 8 'hygiene checks' + the month/blank-column checks requested).

Every finding is written to recon_exceptions with category = 'HYGIENE' and points at the dataset
and the exact row number of the uploaded file.

Every check is also logged once per dataset in recon_check_log - with its readable name, what it tests,
the column(s) it looked at, how many records it checked / failed and PASS / FAIL / NOT RUN - so the Excel
output can list all checks, including the ones that found nothing.

Column names are not fixed: they come from the column mapping chosen on the Run screen (mapping.py).
"""
from psycopg import sql

import config
import db
import mapping as colmap
from schema import DATASETS

INS = sql.SQL("""INSERT INTO recon_exceptions
    (run_id, category, exception_type, dataset, row_no, packet_id, order_release_id, amount_1, amount_2, difference, description) """)

# code -> (readable name, what the check tests).  The readable name is the label used everywhere:
# screens, the 'Hygiene Checks' sheet and the row-level sheets of the Excel output.
CHECKS = {
    "EMPTY_DATASET": ("Empty dataset",
                      "A file must be loaded for the dataset. Fails when the table has no rows."),
    "BLANK_COLUMN": ("Completely blank column",
                     "Columns that are empty in every row of the dataset."),
    "BLANK_KEY_FIELD": ("Blank join key",
                        "Rows where the join key (Packet ID / Order release ID) is empty; these rows cannot be reconciled."),
    "BLANK_AMOUNT_FIELD": ("Blank amount",
                           "Rows where the amount used for net sales / net settlement is empty."),
    "NON_NUMERIC_AMOUNT": ("Amount is not a number",
                           "Amount columns that are filled in but cannot be read as a number."),
    "UNPARSEABLE_DATE": ("Date cannot be read",
                         "Date columns that are filled in but cannot be read as a date."),
    "DATE_OUTSIDE_RECON_MONTH": ("Date outside reconciliation month",
                                 "Rows whose period date does not fall in the selected reconciliation month."),
    "DUPLICATE_KEY_SAME_STATUS": ("Duplicate key with the same order status",
                                  "The same join key appears on more than one row with the same order status."),
    "DUPLICATE_KEY": ("Duplicate key",
                      "The same join key appears on more than one row."),
    "RETURN_AMOUNT_MISMATCH": ("Return amount differs from sale amount",
                               "Return rows whose amount is not equal to the amount of the forward sale."),
    "RETURN_SKU_MISMATCH": ("Returned SKU differs from ordered SKU",
                            "Return rows whose SKU is not the SKU of the forward sale."),
    "RETURN_OUTSIDE_WINDOW": ("Return outside the return window",
                              "Return rows where return date minus shipped date is more than the return window."),
    "MULTIPLE_RETURNS_PER_PACKET": ("Multiple return rows for one packet",
                                    "The same Packet ID appears on more than one row of the return file."),
    "RETURN_WITHOUT_FORWARD_SALE": ("Return without a forward sale",
                                    "Return rows whose Packet ID does not exist in Sales - Forward."),
    "ID_SCIENTIFIC_NOTATION": ("ID in scientific notation",
                               "Join key looks like 1.23457E+15: Excel converted a long ID to a number and the real ID is lost. "
                               "Fix the source file (format the column as Text) and upload again."),
    "EXACT_DUPLICATE_ROW": ("Exact duplicate row",
                            "Row identical in every column to another row of the same file and month "
                            "(e.g. the same file appended twice) - its amount is counted twice."),
    "KEY_DIFFERS_BY_LEADING_ZEROS": ("Key differs only by leading zeros",
                                     "Sales key with no settlement (or the reverse) where the other file has the same key "
                                     "with / without leading zeros (00123 vs 123)."),
    "PACKET_IN_BOTH_RT_AND_RTO": ("Packet in both RT and RTO",
                                  "Packets present in both return files; only the RT amount is deducted from net sales."),
}


def check_name(code):
    return CHECKS[code][0] if code in CHECKS else None


class CheckLog:
    """Writes one line per check per dataset to recon_check_log."""

    def __init__(self, cur, run_id, category, names):
        self.cur, self.run_id, self.category, self.names = cur, run_id, category, names
        cur.execute("SELECT coalesce(max(seq), 0) AS s FROM recon_check_log WHERE run_id = %s", (run_id,))
        self.seq = cur.fetchone()["s"]

    def add(self, code, dataset, columns=None, checked=None, skipped=None, note=None):
        """Log the outcome of a check that has just run (or `skipped` = reason it could not run).

        Records failed = distinct file rows with a finding; for checks that are not row based
        (blank columns, net-level reconciliation checks) it is the number of findings.
        """
        name, description = self.names[code]
        if skipped:
            failed, status, note = None, "NOT RUN", skipped
        else:
            self.cur.execute("""SELECT count(*) AS n, count(DISTINCT row_no) AS r FROM recon_exceptions
                                WHERE run_id = %s AND category = %s AND exception_type = %s
                                  AND dataset IS NOT DISTINCT FROM %s""",
                             (self.run_id, self.category, code, dataset))
            c = self.cur.fetchone()
            failed = c["r"] or c["n"]
            status = "FAIL" if failed else "PASS"
        self.seq += 1
        if isinstance(columns, (list, tuple)):
            columns = ", ".join(c for c in columns if c)
        self.cur.execute("""INSERT INTO recon_check_log
            (run_id, seq, category, check_code, check_name, description, dataset, columns_checked,
             records_checked, records_failed, status, note) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)""",
            (self.run_id, self.seq, self.category, code, name, description, dataset, columns or None,
             checked, failed, status, note))


def _key_expr(m, dataset):
    """Expressions giving packet_id / order_release_id for a row of the dataset (both when available)."""
    key = sql.SQL("t.{}").format(m.col(dataset, "key"))
    if dataset.startswith("settlement"):
        pref = m.col(dataset, "packet_ref")
        return (sql.SQL("t.{}").format(pref) if pref else sql.SQL("NULL")), key
    return key, sql.SQL("NULL")


def _with_mapped(configured, table_cols, *mapped):
    """Configured list of columns to validate + the mapped ones, limited to columns the table really has."""
    out = [c for c in configured if c in table_cols]
    for c in mapped:
        if c and c in table_cols and c not in out:
            out.append(c)
    return out


def run_hygiene(cur, run_id, recon_month, return_window_days, m=None, recon_month_to=None):
    """recon_month / recon_month_to: python dates (first day of the first / last selected month).
    m: mapping.Mapping (default = standard names)."""
    recon_month_to = recon_month_to or recon_month
    span = (f"{recon_month:%b %Y}" if recon_month_to == recon_month
            else f"{recon_month:%b %Y} - {recon_month_to:%b %Y}") if recon_month else ""
    m = m or colmap.default_mapping(cur)
    log = CheckLog(cur, run_id, "HYGIENE", CHECKS)
    totals = {}

    for dataset, spec in DATASETS.items():
        table_cols = db.existing_columns(cur, dataset)
        data_cols = [c for c in table_cols if c not in ("_row_no", "_source_file", "_period")]
        pk, ok = _key_expr(m, dataset)
        key_name, amount_name = m.name(dataset, "key"), m.name(dataset, "amount")
        has_dup_check = dataset in ("sales_forward", "settlement_forward")
        status_col = m.name(dataset, "status") if has_dup_check else None
        dup_code = "DUPLICATE_KEY_SAME_STATUS" if status_col else "DUPLICATE_KEY"

        cur.execute(sql.SQL("SELECT count(*) AS n FROM {}").format(db.q(dataset)))
        total = totals[dataset] = cur.fetchone()["n"]
        if total == 0:
            cur.execute(INS + sql.SQL("VALUES (%s,'HYGIENE','EMPTY_DATASET',%s,NULL,NULL,NULL,NULL,NULL,NULL,%s)"),
                        (run_id, dataset, f"No rows loaded for {spec['label']}"))
            log.add("EMPTY_DATASET", dataset, checked=0)
            for code in ("BLANK_COLUMN", "BLANK_KEY_FIELD", "BLANK_AMOUNT_FIELD", "NON_NUMERIC_AMOUNT",
                         "UNPARSEABLE_DATE", "DATE_OUTSIDE_RECON_MONTH", "ID_SCIENTIFIC_NOTATION",
                         "EXACT_DUPLICATE_ROW") + ((dup_code,) if has_dup_check else ()):
                log.add(code, dataset, skipped="No rows loaded for this dataset")
            continue
        log.add("EMPTY_DATASET", dataset, checked=total)

        # 1. Completely blank columns (column present in table but empty in every loaded row)
        blank_parts = [sql.SQL("count(*) FILTER (WHERE {c} IS NULL OR btrim({c}) = '') AS {a}").format(
            c=db.q(c), a=db.q(f"b{i}")) for i, c in enumerate(data_cols)]
        cur.execute(sql.SQL("SELECT ") + sql.SQL(", ").join(blank_parts) + sql.SQL(" FROM {}").format(db.q(dataset)))
        counts = cur.fetchone()
        for i, c in enumerate(data_cols):
            if counts[f"b{i}"] == total:
                cur.execute(INS + sql.SQL("VALUES (%s,'HYGIENE','BLANK_COLUMN',%s,NULL,NULL,NULL,NULL,NULL,NULL,%s)"),
                            (run_id, dataset, f"Column '{c}' is blank in all {total} rows of {spec['label']}"))
        log.add("BLANK_COLUMN", dataset, columns=f"All {len(data_cols)} columns of the table", checked=len(data_cols),
                note="Counts are columns, not rows (includes standard columns the file did not contain)")

        # 2. Blank key / amount field on individual rows
        for col, etype in ((key_name, "BLANK_KEY_FIELD"), (amount_name, "BLANK_AMOUNT_FIELD")):
            cur.execute(INS + sql.SQL("""
                SELECT %s,'HYGIENE',%s,%s,t.{rn},{pk},{ok},NULL,NULL,NULL,
                       %s FROM {tbl} t WHERE t.{c} IS NULL OR btrim(t.{c}) = ''""").format(
                rn=db.q("_row_no"), pk=pk, ok=ok, tbl=db.q(dataset), c=db.q(col)),
                (run_id, etype, dataset, f"'{col}' is blank in {spec['label']}"))
            log.add(etype, dataset, columns=col, checked=total)

        # 3. Amount columns that are populated but not numeric
        amount_cols = _with_mapped(config.AMOUNT_COLUMNS.get(dataset, []), table_cols, amount_name)
        for col in amount_cols:
            cur.execute(INS + sql.SQL("""
                SELECT %s,'HYGIENE','NON_NUMERIC_AMOUNT',%s,t.{rn},{pk},{ok},NULL,NULL,NULL,
                       %s || t.{c} FROM {tbl} t
                WHERE btrim(coalesce(t.{c},'')) <> '' AND try_numeric(t.{c}) IS NULL""").format(
                rn=db.q("_row_no"), pk=pk, ok=ok, tbl=db.q(dataset), c=db.q(col)),
                (run_id, dataset, f"'{col}' is not numeric: "))
        log.add("NON_NUMERIC_AMOUNT", dataset, columns=amount_cols, checked=total)

        # 4. Date columns that are populated but not parseable
        date_cols = _with_mapped(config.DATE_COLUMNS.get(dataset, []), table_cols, m.name(dataset, "period_date"),
                                 m.name(dataset, "ship_date") if dataset in config.RETURN_WINDOW_DATES else None,
                                 m.name(dataset, "return_date") if dataset in config.RETURN_WINDOW_DATES else None)
        for col in date_cols:
            cur.execute(INS + sql.SQL("""
                SELECT %s,'HYGIENE','UNPARSEABLE_DATE',%s,t.{rn},{pk},{ok},NULL,NULL,NULL,
                       %s || t.{c} FROM {tbl} t
                WHERE btrim(coalesce(t.{c},'')) <> '' AND try_date(t.{c}) IS NULL""").format(
                rn=db.q("_row_no"), pk=pk, ok=ok, tbl=db.q(dataset), c=db.q(col)),
                (run_id, dataset, f"'{col}' could not be read as a date: "))
        log.add("UNPARSEABLE_DATE", dataset, columns=date_cols, checked=total)

        # 5. Month check: period date must fall in the selected reconciliation month
        mcol = m.name(dataset, "period_date")
        if mcol and recon_month:
            cur.execute(INS + sql.SQL("""
                SELECT %s,'HYGIENE','DATE_OUTSIDE_RECON_MONTH',%s,t.{rn},{pk},{ok},NULL,NULL,NULL,
                       %s || t.{c} FROM {tbl} t
                WHERE try_date(t.{c}) IS NOT NULL
                  AND ((t.{p} >= %s AND t.{p} <= %s) OR coalesce(t.{p}, '') = '')   -- rows uploaded as the selected months' data
                  AND (date_trunc('month', try_date(t.{c})) < date_trunc('month', %s::date)
                       OR date_trunc('month', try_date(t.{c})) > date_trunc('month', %s::date))""").format(
                rn=db.q("_row_no"), pk=pk, ok=ok, tbl=db.q(dataset), c=db.q(mcol), p=db.q("_period")),
                (run_id, dataset, f"'{mcol}' is outside {span}: ", recon_month.strftime("%Y-%m"),
                 recon_month_to.strftime("%Y-%m"), recon_month, recon_month_to))
            log.add("DATE_OUTSIDE_RECON_MONTH", dataset, columns=mcol, checked=total)
        else:
            log.add("DATE_OUTSIDE_RECON_MONTH", dataset,
                    skipped="No 'Period date' column mapped" if not mcol else "No reconciliation month given")

        # 6. Duplicate key basis status (BRD: "Duplicate Order ID basis order Status")
        if has_dup_check:
            if status_col:
                cur.execute(INS + sql.SQL("""
                    SELECT %s,'HYGIENE','DUPLICATE_KEY_SAME_STATUS',%s,t.{rn},{pk},{ok},NULL,NULL,NULL,
                           %s || t.{k} || ' / status=' || coalesce(t.{s},'') || ' (' || d.n || ' rows)'
                    FROM {tbl} t JOIN (SELECT {k} k, upper(btrim(coalesce({s},''))) s, count(*) n FROM {tbl}
                                       WHERE {k} IS NOT NULL AND btrim({k}) <> ''
                                       GROUP BY 1,2 HAVING count(*) > 1) d
                      ON d.k = t.{k} AND d.s = upper(btrim(coalesce(t.{s},'')))""").format(
                    rn=db.q("_row_no"), pk=pk, ok=ok, tbl=db.q(dataset), k=db.q(key_name), s=db.q(status_col)),
                    (run_id, dataset, f"Duplicate {key_name} with the same {status_col}: "))
                log.add(dup_code, dataset, columns=[key_name, status_col], checked=total)
            else:
                cur.execute(INS + sql.SQL("""
                    SELECT %s,'HYGIENE','DUPLICATE_KEY',%s,t.{rn},{pk},{ok},NULL,NULL,NULL,
                           %s || t.{k} || ' (' || d.n || ' rows)'
                    FROM {tbl} t JOIN (SELECT {k} k, count(*) n FROM {tbl}
                                       WHERE {k} IS NOT NULL AND btrim({k}) <> ''
                                       GROUP BY 1 HAVING count(*) > 1) d ON d.k = t.{k}""").format(
                    rn=db.q("_row_no"), pk=pk, ok=ok, tbl=db.q(dataset), k=db.q(key_name)),
                    (run_id, dataset, f"Duplicate {key_name}: "))
                log.add(dup_code, dataset, columns=key_name, checked=total,
                        note="No 'Order status' column mapped, so duplicates are checked on the key alone")

        # 7. Long IDs that Excel turned into scientific notation (1.23457E+15)
        cur.execute(INS + sql.SQL("""
            SELECT %s,'HYGIENE','ID_SCIENTIFIC_NOTATION',%s,t.{rn},{pk},{ok},NULL,NULL,NULL,
                   %s || t.{k} FROM {tbl} t
            WHERE btrim(t.{k}) ~* '^[0-9](\\.[0-9]+)?E\\+?[0-9]+$'""").format(
            rn=db.q("_row_no"), pk=pk, ok=ok, tbl=db.q(dataset), k=db.q(key_name)),
            (run_id, dataset, f"'{key_name}' is in scientific notation: "))
        log.add("ID_SCIENTIFIC_NOTATION", dataset, columns=key_name, checked=total)

        # 8. Exact duplicate rows (every column equal, same data month) - all copies after the first are flagged
        cur.execute(INS + sql.SQL("""
            SELECT %s,'HYGIENE','EXACT_DUPLICATE_ROW',%s,x.rn,x.pk,x.ok,NULL,NULL,NULL,
                   'Same as file row ' || x.first_rn || ' (' || x.n || ' identical rows)'
            FROM (SELECT t.{rn} AS rn, {pk} AS pk, {ok} AS ok,
                         row_number() OVER w AS i, min(t.{rn}) OVER w AS first_rn, count(*) OVER w AS n
                  FROM {tbl} t
                  WINDOW w AS (PARTITION BY coalesce(t.{p}, ''),
                                            md5((to_jsonb(t) - '_row_no' - '_source_file')::text))) x
            WHERE x.i > 1""").format(rn=db.q("_row_no"), pk=pk, ok=ok, tbl=db.q(dataset), p=db.q("_period")),
            (run_id, dataset))
        log.add("EXACT_DUPLICATE_ROW", dataset, columns="All columns", checked=total)

    # ---- Keys that only differ by leading zeros between sales and settlement (00123 vs 123)
    sk, tk = m.col("sales_forward", "key"), m.col("settlement_forward", "key")
    if totals.get("sales_forward") and totals.get("settlement_forward"):
        cur.execute(INS + sql.SQL("""
            SELECT %s,'HYGIENE','KEY_DIFFERS_BY_LEADING_ZEROS','sales_forward',s.{rn},s.{sk},t.{tk},NULL,NULL,NULL,
                   'Sales ' || s.{sk} || ' vs settlement ' || t.{tk}
            FROM sales_forward s
            JOIN settlement_forward t ON ltrim(btrim(t.{tk}), '0') = ltrim(btrim(s.{sk}), '0') AND btrim(t.{tk}) <> btrim(s.{sk})
            WHERE ltrim(btrim(s.{sk}), '0') <> ''
              AND NOT EXISTS (SELECT 1 FROM settlement_forward t2 WHERE t2.{tk} = s.{sk})""").format(
            rn=db.q("_row_no"), sk=sk, tk=tk), (run_id,))
        log.add("KEY_DIFFERS_BY_LEADING_ZEROS", "sales_forward",
                columns=[m.name("sales_forward", "key"), f"Settlement - Forward.{m.name('settlement_forward', 'key')}"],
                checked=totals["sales_forward"])
    else:
        log.add("KEY_DIFFERS_BY_LEADING_ZEROS", "sales_forward", skipped="Sales - Forward or Settlement - Forward is empty")

    # ---- Cross-dataset checks on the return files (BRD: return amount, return SKU, return window) ----
    fw = db.q("sales_forward")
    fk, fa, fsku = (m.col("sales_forward", r) for r in ("key", "amount", "sku"))
    fa_name, fsku_name = m.name("sales_forward", "amount"), m.name("sales_forward", "sku")
    for rdataset in ("sales_rt", "sales_rto"):
        label = DATASETS[rdataset]["label"]
        total = totals[rdataset]
        rk, ra, rsku = (m.col(rdataset, r) for r in ("key", "amount", "sku"))
        rk_name, ra_name, rsku_name = (m.name(rdataset, r) for r in ("key", "amount", "sku"))
        ship_name, ret_name = m.name(rdataset, "ship_date"), m.name(rdataset, "return_date")
        fmt = dict(rn=db.q("_row_no"), rt=db.q(rdataset), fw=fw, rk=rk, ra=ra, fk=fk, fa=fa)
        if total == 0:
            for code in ("RETURN_AMOUNT_MISMATCH", "RETURN_SKU_MISMATCH", "RETURN_OUTSIDE_WINDOW",
                         "MULTIPLE_RETURNS_PER_PACKET", "RETURN_WITHOUT_FORWARD_SALE"):
                log.add(code, rdataset, skipped="No rows loaded for this dataset")
            continue

        # Return amount is not the same as ordered amount
        cur.execute(INS + sql.SQL("""
            SELECT %s,'HYGIENE','RETURN_AMOUNT_MISMATCH',%s,r.{rn},r.{rk},NULL,
                   try_numeric(f.{fa}), try_numeric(r.{ra}),
                   try_numeric(r.{ra}) - try_numeric(f.{fa}),
                   %s || coalesce(f.{fa},'') || ' vs return ' || coalesce(r.{ra},'')
            FROM {rt} r JOIN {fw} f ON f.{fk} = r.{rk}
            WHERE try_numeric(f.{fa}) IS DISTINCT FROM try_numeric(r.{ra})""").format(**fmt),
            (run_id, rdataset, f"{label}: forward {fa_name} "))
        log.add("RETURN_AMOUNT_MISMATCH", rdataset, columns=[ra_name, f"Sales - Forward.{fa_name}"], checked=total)

        # Returned SKU aligned against order SKU
        if rsku and fsku:
            cur.execute(INS + sql.SQL("""
                SELECT %s,'HYGIENE','RETURN_SKU_MISMATCH',%s,r.{rn},r.{rk},NULL,NULL,NULL,NULL,
                       %s || coalesce(f.{fsku},'') || %s || coalesce(r.{rsku},'')
                FROM {rt} r JOIN {fw} f ON f.{fk} = r.{rk}
                WHERE btrim(coalesce(f.{fsku},'')) <> btrim(coalesce(r.{rsku},''))""").format(
                fsku=fsku, rsku=rsku, **fmt),
                (run_id, rdataset, f"{label}: ordered {fsku_name} ", f" vs returned {rsku_name} "))
            log.add("RETURN_SKU_MISMATCH", rdataset, columns=[rsku_name, f"Sales - Forward.{fsku_name}"], checked=total)
        else:
            log.add("RETURN_SKU_MISMATCH", rdataset, skipped="No 'SKU' column mapped on " +
                    ("this file" if not rsku else "Sales - Forward"))

        # Timeline adherence to product return window
        if ship_name and ret_name and return_window_days:
            cur.execute(INS + sql.SQL("""
                SELECT %s,'HYGIENE','RETURN_OUTSIDE_WINDOW',%s,r.{rn},r.{rk},NULL,
                       NULL,NULL, (try_date(r.{ret}) - try_date(r.{ship})),
                       %s || (try_date(r.{ret}) - try_date(r.{ship})) || ' days (' || r.{ship} || ' -> ' || r.{ret} || ')'
                FROM {rt} r
                WHERE try_date(r.{ret}) IS NOT NULL AND try_date(r.{ship}) IS NOT NULL
                  AND (try_date(r.{ret}) - try_date(r.{ship})) > %s""").format(
                ret=db.q(ret_name), ship=db.q(ship_name), **fmt),
                (run_id, rdataset, f"{label}: return after ", return_window_days))
            log.add("RETURN_OUTSIDE_WINDOW", rdataset, columns=[ship_name, ret_name], checked=total,
                    note=f"Return window = {return_window_days} days")
        else:
            log.add("RETURN_OUTSIDE_WINDOW", rdataset, skipped=(
                "Return window is 0 / not set" if ship_name and ret_name
                else "No 'Shipped date' / 'Return date' column mapped"))

        # Multiple return rows for one packet
        cur.execute(INS + sql.SQL("""
            SELECT %s,'HYGIENE','MULTIPLE_RETURNS_PER_PACKET',%s,r.{rn},r.{rk},NULL,NULL,NULL,NULL,
                   %s || d.n || ' rows'
            FROM {rt} r JOIN (SELECT {rk} AS k, count(*) n FROM {rt}
                              WHERE {rk} IS NOT NULL AND btrim({rk}) <> ''
                              GROUP BY 1 HAVING count(*) > 1) d ON d.k = r.{rk}""").format(**fmt),
            (run_id, rdataset, f"{label}: {rk_name} appears in "))
        log.add("MULTIPLE_RETURNS_PER_PACKET", rdataset, columns=rk_name, checked=total)

        # Return without a forward sale
        cur.execute(INS + sql.SQL("""
            SELECT %s,'HYGIENE','RETURN_WITHOUT_FORWARD_SALE',%s,r.{rn},r.{rk},NULL,
                   NULL, try_numeric(r.{ra}), NULL, %s
            FROM {rt} r LEFT JOIN {fw} f ON f.{fk} = r.{rk}
            WHERE f.{fk} IS NULL AND r.{rk} IS NOT NULL AND btrim(r.{rk}) <> ''""").format(**fmt),
            (run_id, rdataset, f"{label}: {rk_name} not found in Sales - Forward"))
        log.add("RETURN_WITHOUT_FORWARD_SALE", rdataset, columns=[rk_name, f"Sales - Forward.{m.name('sales_forward', 'key')}"],
                checked=total)

    # Packet present in BOTH RT and RTO (only one return should be applied)
    cur.execute(INS + sql.SQL("""
        SELECT %s,'HYGIENE','PACKET_IN_BOTH_RT_AND_RTO','sales_rt',rt.{rn},rt.{rtk},NULL,
               try_numeric(rt.{rta}), try_numeric(rto.{rtoa}), NULL,
               'packet_id exists in both RT and RTO; only RT was applied to net sales'
        FROM sales_rt rt JOIN sales_rto rto ON rto.{rtok} = rt.{rtk}""").format(
        rn=db.q("_row_no"), rtk=m.col("sales_rt", "key"), rta=m.col("sales_rt", "amount"),
        rtok=m.col("sales_rto", "key"), rtoa=m.col("sales_rto", "amount")), (run_id,))
    log.add("PACKET_IN_BOTH_RT_AND_RTO", "sales_rt",
            columns=[m.name("sales_rt", "key"), f"Sales - RTO.{m.name('sales_rto', 'key')}"], checked=totals["sales_rt"])

    cur.execute("SELECT count(*) AS n FROM recon_exceptions WHERE run_id = %s AND category = 'HYGIENE'", (run_id,))
    return cur.fetchone()["n"]
