"""Generates a small, fully-known test data set for the Sales vs Settlement module + its answer key.

    python make_test_data.py

Writes (next to this script):
    2019-01/<dataset>.csv   five files to upload with data month Jan 2019
    2019-02/<dataset>.csv   five files to upload with data month Feb 2019
    EXPECTED_RESULTS.md     what every run must show (KPIs, status per key, exceptions, hygiene, open items)
    expected_results.xlsx   the same, as sheets (easy to compare with the app's Excel download)

Every column of the real headers (schema.py) is filled, so no 'Completely blank column' findings come from the
test rows themselves. The expected answer is computed by an independent re-implementation of the rules in
recon.py / hygiene.py / carry.py (below) AND checked against the hand-written expectation of every scenario,
so a mismatch between the two stops the script.

Run settings to use in the app: platform TESTMART, tolerance 0.01, return window 30, tax tolerance 1.00, T+2.
"""
import calendar
import csv
import os
import re
import sys
from collections import defaultdict
from datetime import date, timedelta
from decimal import Decimal as D

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "backend"))
from schema import DATASETS, DATASET_ORDER  # noqa: E402

PLATFORM = "TESTMART"
TOL, RETURN_WINDOW, TAX_TOL, LAG = D("0.01"), 30, D("1.00"), 2
JAN, FEB = "2019-01", "2019-02"

# --------------------------------------------------------------------------------------------- scenarios
# Each scenario: key, what it tests, rows per file per month, and the status expected in each run.
# Row dicts only carry what matters; every other column is filled by fill_row().
S = []


def sc(key, title, rows, expect, note=""):
    S.append({"key": key, "title": title, "rows": rows, "expect": expect, "note": note})


def d(m, day):
    y, mo = map(int, m.split("-"))
    return date(y, mo, day)


def sale(m, amt, day, **kw):           # Sales - Forward
    return ("sales_forward", m, dict(amt=amt, day=d(m, day) if isinstance(day, int) else day, **kw))


def rt(m, amt, ship, ret, **kw):       # Sales - RT
    return ("sales_rt", m, dict(amt=amt, ship=ship, ret=ret, **kw))


def rto(m, amt, ship, ret, **kw):      # Sales - RTO
    return ("sales_rto", m, dict(amt=amt, ship=ship, ret=ret, **kw))


def fwd(m, amt, day, **kw):            # Settlement - Forward
    return ("settlement_forward", m, dict(amt=amt, day=d(m, day) if isinstance(day, int) else day, **kw))


def rev(m, amt, day, **kw):            # Settlement - Reverse
    return ("settlement_reverse", m, dict(amt=amt, day=d(m, day) if isinstance(day, int) else day, **kw))


J, F = JAN, FEB
M, SH, SS, RP, SNS, STNS, RSWF = ("MATCHED", "SALES_HIGHER_THAN_SETTLEMENT", "SETTLEMENT_HIGHER_THAN_SALES",
                                  "RETURN_PROCESSED_SETTLEMENT_NOT_ADJUSTED", "SALES_NOT_IN_SETTLEMENT",
                                  "SETTLEMENT_NOT_IN_SALES", "REVERSE_SETTLEMENT_WITHOUT_FORWARD")

sc("TST-A01", "Simple match", [sale(J, "1000", 5), fwd(J, "1000", 6)], {J: M})
sc("TST-A02", "Packet with 2 item rows on both sides (summed per key)",
   [sale(J, "600", 7), sale(J, "400", 7), fwd(J, "600", 8), fwd(J, "400", 8)], {J: M},
   "Hygiene: duplicate key (same status) on both sales rows, duplicate key on both settlement rows.")
sc("TST-A03", "Difference inside tolerance (0.005 <= 0.01)", [sale(J, "500.00", 9), fwd(J, "499.995", 10)], {J: M})
sc("TST-A04", "Settled short in Jan, balance (50) settled in Feb",
   [sale(J, "1200", 10), fwd(J, "1150", 11), fwd(F, "50", d(J, 15))], {J: SH, F: M},
   "Feb top-up row keeps packing_date 15 Jan -> 'Date outside reconciliation month' in Feb.")
sc("TST-A05", "Settled in excess, never corrected", [sale(J, "800", 11), fwd(J, "820", 12)], {J: SS, F: SS})
sc("TST-A06", "Customer return (RT) fully reversed", [sale(J, "900", 3), rt(J, "900", d(J, 4), d(J, 14)),
                                                     fwd(J, "900", 4), rev(J, "900", 15)], {J: M})
sc("TST-A07", "RTO fully reversed", [sale(J, "700", 4), rto(J, "700", d(J, 5), d(J, 12)),
                                     fwd(J, "700", 5), rev(J, "700", 13)], {J: M})
sc("TST-A08", "Return processed, reverse settlement only arrives in Feb",
   [sale(J, "650", 6), rt(J, "650", d(J, 7), d(J, 16)), fwd(J, "650", 7), rev(F, "650", 5)], {J: RP, F: M},
   "Jan exception: return in sales but no reverse settlement.")
sc("TST-A09", "Partial return (RT 400 of 1000), reversed correctly",
   [sale(J, "1000", 8), rt(J, "400", d(J, 9), d(J, 20)), fwd(J, "1000", 9), rev(J, "400", 21)], {J: M},
   "Hygiene: return amount differs from sale amount.")
sc("TST-A10", "Sale never settled", [sale(J, "300", 12)], {J: SNS, F: SNS})
sc("TST-A11", "Sale on 31 Jan, settles in Feb (T+2 awaiting)",
   [sale(J, "450", 31), fwd(F, "450", d(J, 31))], {J: SNS, F: M},
   "Jan: awaiting settlement (T+2). Feb row keeps packing_date 31 Jan -> date outside month in Feb.")
sc("TST-A12", "Settlement with no sale", [fwd(J, "450", 13)], {J: STNS, F: STNS})
sc("TST-A13", "Reverse settlement with no forward and no sale", [rev(J, "250", 14)], {J: RSWF},
   "Own result row, never an open item.")
sc("TST-A14", "Reverse settlement but no return in sales",
   [sale(J, "1100", 14), fwd(J, "1100", 15), rev(J, "100", 18)], {J: SH, F: SH},
   "Exception: reverse settlement but no return in sales.")
sc("TST-A15", "Two reverse settlement rows for one order",
   [sale(J, "1500", 15), rt(J, "1500", d(J, 16), d(J, 22)), fwd(J, "1500", 16),
    rev(J, "750", 23, return_id="R-A15-1"), rev(J, "750", 23, return_id="R-A15-2")], {J: M},
   "Exception: multiple reverse settlements.")
sc("TST-A16", "Packet in both RT and RTO (only RT deducted)",
   [sale(J, "600", 16), rt(J, "600", d(J, 17), d(J, 24)), rto(J, "600", d(J, 17), d(J, 24)),
    fwd(J, "600", 17), rev(J, "600", 25)], {J: M}, "Hygiene: packet in both RT and RTO.")
sc("TST-A17", "Sale + RT, only a reverse settlement (no forward)",
   [sale(J, "400", 17), rt(J, "400", d(J, 18), d(J, 25)), rev(J, "400", 26)], {J: RP, F: RP},
   "Net settlement = 0 - 400 = -400. Exception: reverse settlement without forward settlement.")
sc("TST-A18", "Settlement packet_id differs from order_release_id",
   [sale(J, "750", 18), fwd(J, "750", 19, packet_id="TST-A18X")], {J: M},
   "Exception: packet ID not equal to order release ID.")
sc("TST-A19", "Tax variance on settlement (IGST 60 vs 1000 x 5% = 50)",
   [sale(J, "1000", 19), fwd(J, "1000", 20, igst="60.00")], {J: M}, "Exception: tax variance (diff 10 > 1.00).")
sc("TST-A20", "Lower-case ID in settlement file (upper-cased on upload)",
   [sale(J, "550", 20), fwd(J, "550", 21, key="tst-a20", packet_id="tst-a20")], {J: M})
sc("0091001", "Sales key with leading zeros (settlement has 91001)", [sale(J, "820", 21)], {J: SNS, F: SNS},
   "Hygiene: key differs only by leading zeros. Open the CSV only in a text editor (Excel drops the zeros).")
sc("91001", "Settlement key without the leading zeros", [fwd(J, "820", 22)], {J: STNS, F: STNS})
sc("TST-A22", "Same settlement row twice (file appended twice)",
   [sale(J, "200", 22), fwd(J, "200", 23), fwd(J, "200", 23, dup=True)], {J: SS, F: SS},
   "Hygiene: exact duplicate row + duplicate key. 200 counted twice -> settlement 400.")
sc("TST-A23", "Sale packed 28 Dec 2018 but uploaded as Jan data",
   [sale(J, "980", date(2018, 12, 28)), fwd(J, "980", 2)], {J: M}, "Hygiene: date outside reconciliation month.")
sc("TST-A24", "Settled, but on another platform (OTHERMART)",
   [sale(J, "350", 24), fwd(J, "350", 25, platform="OTHERMART")], {J: SNS, F: SNS},
   "The platform filter TESTMART leaves the settlement out.")
sc("TST-A25", "Matched in Jan; RT 48 days after shipping arrives in Feb",
   [sale(J, "900", 2), fwd(J, "900", 3), rt(F, "900", d(J, 3), d(F, 20)), rev(F, "900", 21)], {J: M, F: M},
   "Feb hygiene: return outside the 30-day window. Feb run pulls the Jan rows of the key.")
sc("TST-B01", "Feb simple match", [sale(F, "1300", 10), fwd(F, "1300", 11)], {F: M})
sc("TST-B02", "Feb settled short by 20", [sale(F, "2000", 11), fwd(F, "1980", 12)], {F: SH})
sc("TST-B03", "Feb RTO fully reversed", [sale(F, "700", 3), rto(F, "700", d(F, 4), d(F, 10)),
                                         fwd(F, "700", 4), rev(F, "700", 11)], {F: M})
sc("TST-B04", "Return on 28 Feb, reverse not yet settled (T+2 awaiting)",
   [sale(F, "1250", 5), fwd(F, "1250", 6), rt(F, "1250", d(F, 6), d(F, 28))], {F: RP},
   "Awaiting (T+2). Exception: return in sales but no reverse settlement.")

# --------------------------------------------------------------------------------------------- files
NUMERIC = re.compile(r"(amt|amount|value|charge|mrp|rate|fee|discount|cashback|tcs|tds|credit|wallet|giftcard|"
                     r"mynts|refund|quantity|qty|commission|deduction|payment|percentage|tax|cess|points|phonepe|"
                     r"revenue|settlement|net_amount|redeemed|_used|tax_user_rec)", re.I)
DATEISH = re.compile(r"(date|starts_from|ends_on|last_modified_on)", re.I)
UNIQUE = {"item_id", "order_line_id", "pps_item_id", "core_item_id", "shipment_id", "invoice_number",
          "seller_order_id", "return_id", "order_id", "row_num"}
counter = [0]


def fill_row(ds, key, sku, r):
    """One CSV row with every column of the real header filled."""
    counter[0] += 1
    n = counter[0]
    amt, day = r["amt"], r.get("day") or r.get("ret")
    out = {}
    for c in DATASETS[ds]["columns"]:
        lc = c.lower()
        if lc in UNIQUE:
            v = f"{c[:3].upper()}{n:05d}"
        elif lc == "sku_id" or lc == "sku_code":
            v = sku
        elif DATEISH.search(c):
            v = (day - timedelta(days=1)).isoformat()
        elif NUMERIC.search(c) and lc not in ("seller_gstin", "tax_seller_gstin"):
            v = "0"
        else:
            v = f"{c[:12]}-x"
        out[c] = v
    if "state" in out:
        out["state"] = "KA"
    if ds.startswith("sales"):
        out["packet_id"] = key
        out["shipment_value"] = amt
        out["seller_name"] = "Test Seller"
        if ds == "sales_forward":
            out["order_status"] = "DELIVERED"
            out["order_created_date"] = (day - timedelta(days=1)).isoformat()
            out["order_packed_date"] = day.isoformat()
            out["order_shipped_date"] = (day + timedelta(days=1)).isoformat()
            out["MRP"] = out["Net_Amount"] = out["base_amount"] = amt
        else:
            out["order_shipped_date"] = r["ship"].isoformat()
            ret_col = "fr_refunded_date" if ds == "sales_rt" else "order_rto_date"
            out[ret_col] = r["ret"].isoformat()
            out["order_status"] = "RT" if ds == "sales_rt" else "RTO"
            out["MRP"] = out["Net_Amount"] = out["base_amount"] = amt
    else:
        k = r.get("key", key)
        out["Platform"] = r.get("platform", PLATFORM)
        out["order_release_id"] = k
        out["packet_id"] = r.get("packet_id", k)
        out["customer_paid_amount"] = amt
        out["packing_date"] = day.isoformat()
        out["delivery_date"] = (day + timedelta(days=2)).isoformat()
        out["return_date"] = day.isoformat()
        a = D(amt)
        out["taxable_amount"] = str(a)
        out["total_tax_rate"] = "5"
        out["igst_amount"] = r.get("igst", str((a * D("0.05")).quantize(D("0.01"))))
        out["cgst_amount"] = out["sgst_amount"] = "0"
        out["total_actual_settlement"] = out["total_expected_settlement"] = str(a)
        if ds == "settlement_reverse":
            out["return_id"] = r.get("return_id", f"R-{k}")
    return out


def build_files():
    files = defaultdict(list)          # (month, ds) -> rows
    for s in S:
        key = s["key"]
        sku = f"SKU-{key}"
        for ds, m, r in s["rows"]:
            if r.get("dup"):
                files[(m, ds)].append(dict(files[(m, ds)][-1]))     # identical copy of the previous row
            else:
                files[(m, ds)].append(fill_row(ds, key, sku, r))
    for m in (JAN, FEB):
        os.makedirs(os.path.join(HERE, m), exist_ok=True)
        for ds in DATASET_ORDER:
            rows = files[(m, ds)]
            with open(os.path.join(HERE, m, f"{ds}.csv"), "w", newline="", encoding="utf-8") as fh:
                w = csv.DictWriter(fh, fieldnames=DATASETS[ds]["columns"])
                w.writeheader()
                w.writerows(rows)
    return files


# --------------------------------------------------------------------------------------------- reference engine
def load(files):
    """Rows as the loader stores them: ids trimmed + upper-cased, row number (header = row 1), data month."""
    data = {ds: [] for ds in DATASET_ORDER}
    for (m, ds), rows in files.items():
        for i, r in enumerate(rows):
            x = dict(r)
            for c in ("packet_id", "order_release_id"):
                if c in x:
                    x[c] = x[c].strip().upper()
            x["_row_no"], x["_period"], x["_file"] = i + 2, m, f"{m}/{ds}.csv"
            data[ds].append(x)
    return data


KEY = {d: DATASETS[d]["key"] for d in DATASET_ORDER}
AMT = {d: DATASETS[d]["amount"] for d in DATASET_ORDER}
PERIOD_COL = {"sales_forward": "order_packed_date", "sales_rt": "fr_refunded_date", "sales_rto": "order_rto_date",
              "settlement_forward": "packing_date", "settlement_reverse": "return_date"}
RET_DATE = {"sales_rt": "fr_refunded_date", "sales_rto": "order_rto_date"}


def ymd(s):
    return date.fromisoformat(s)


def run(data, month, open_items):
    """One run of month `month` with platform filter PLATFORM. Mutates open_items. Returns the run's answer."""
    plat_ok = lambda ds, r: (not ds.startswith("settlement")) or r["Platform"].strip().upper() == PLATFORM
    mstart = date.fromisoformat(month + "-01")
    # scope
    scope, carried = set(), set()
    for ds in DATASET_ORDER:
        for r in data[ds]:
            if r["_period"] == month and r[KEY[ds]] and plat_ok(ds, r):
                scope.add(r[KEY[ds]])
    for k, o in open_items.items():
        if o["is_open"] and o["first_month"] < mstart:
            scope.add(k)
            carried.add(k)
    sh = {ds: [r for r in data[ds] if r[KEY[ds]] in scope and r["_period"] <= month] for ds in DATASET_ORDER}

    # net sales
    ns = {}
    for r in sh["sales_forward"]:
        k = r["packet_id"]
        ns.setdefault(k, {"fwd": D(0), "rt": None, "rto": None})["fwd"] += D(r["shipment_value"])
    for ds, f in (("sales_rt", "rt"), ("sales_rto", "rto")):
        for r in sh[ds]:
            if r["packet_id"] in ns:
                v = ns[r["packet_id"]]
                v[f] = (v[f] or D(0)) + abs(D(r["shipment_value"]))
    for v in ns.values():
        v["src"] = "RT" if v["rt"] is not None else "RTO" if v["rto"] is not None else "NONE"
        v["ret"] = v["rt"] if v["src"] == "RT" else v["rto"] if v["src"] == "RTO" else D(0)
        v["net"] = v["fwd"] - (v["ret"] or D(0))
    # net settlement
    fw, rv = defaultdict(lambda: [D(0), 0]), defaultdict(lambda: [D(0), 0])
    for r in sh["settlement_forward"]:
        if plat_ok("settlement_forward", r):
            fw[r["order_release_id"]][0] += D(r["customer_paid_amount"])
            fw[r["order_release_id"]][1] += 1
    for r in sh["settlement_reverse"]:
        if plat_ok("settlement_reverse", r):
            rv[r["order_release_id"]][0] += abs(D(r["customer_paid_amount"]))
            rv[r["order_release_id"]][1] += 1
    nt = {}
    for k in set(fw) | set(rv):
        if k in fw or k in ns:
            f, n = fw.get(k, [D(0), 0])
            rr, nr = rv.get(k, [D(0), 0])
            nt[k] = {"fwd": f, "n": n, "rev": rr, "nrev": nr, "net": f - rr}
    # results
    res = {}
    for k in set(ns) | set(nt):
        s, t = ns.get(k), nt.get(k)
        diff = (s["net"] if s else D(0)) - (t["net"] if t else D(0))
        if s is None:
            st = STNS
        elif t is None:
            st = SNS
        elif abs(diff) <= TOL:
            st = M
        elif abs(s["net"]) <= TOL and ((s["rt"] or 0) != 0 or (s["rto"] or 0) != 0):
            st = RP
        elif s["net"] > t["net"]:
            st = SH
        else:
            st = SS
        res[k] = {"key": k, "status": st, "fwd_sales": s and s["fwd"], "ret_src": s and s["src"],
                  "ret": s and s["ret"], "net_sales": s and s["net"], "fwd_set": t and t["fwd"],
                  "rev_set": t and t["rev"], "net_set": t and t["net"], "diff": diff, "exc": set(), "hyg": set()}
    # reconciliation exceptions (one entry per exception row)
    exc = []
    for ds in ("settlement_forward", "settlement_reverse"):
        for r in sh[ds]:
            if r["packet_id"] and r["order_release_id"] and r["packet_id"] != r["order_release_id"]:
                exc.append(("PACKET_ID_NOT_EQUAL_ORDER_RELEASE_ID", r["order_release_id"]))
    fkeys = {r["order_release_id"] for r in sh["settlement_forward"] if plat_ok("settlement_forward", r)}
    for r in sh["settlement_reverse"]:
        if plat_ok("settlement_reverse", r) and r["order_release_id"] not in fkeys:
            exc.append(("REVERSE_SETTLEMENT_WITHOUT_FORWARD", r["order_release_id"], D(r["customer_paid_amount"])))
    for k, t in nt.items():
        if t["nrev"] > 1:
            exc.append(("MULTIPLE_REVERSE_SETTLEMENTS", k))
    for k in set(ns) & set(nt):
        if ns[k]["src"] != "NONE" and nt[k]["nrev"] == 0:
            exc.append(("RETURN_IN_SALES_BUT_NO_REVERSE_SETTLEMENT", k))
        if ns[k]["src"] == "NONE" and nt[k]["nrev"] > 0:
            exc.append(("REVERSE_SETTLEMENT_BUT_NO_RETURN_IN_SALES", k))
    for r in sh["settlement_forward"]:
        tax = D(r["igst_amount"]) + D(r["cgst_amount"]) + D(r["sgst_amount"])
        exp = (D(r["taxable_amount"]) * D(r["total_tax_rate"]) / 100).quantize(D("0.01"))
        if abs(tax - exp) > TAX_TOL:
            exc.append(("TAX_VARIANCE", r["order_release_id"]))
    orphans = defaultdict(D)
    for e in exc:
        if e[0] == "REVERSE_SETTLEMENT_WITHOUT_FORWARD":
            orphans[e[1]] += e[2]
    for k, a in orphans.items():
        if k not in res:
            res[k] = {"key": k, "status": RSWF, "fwd_sales": None, "ret_src": None, "ret": None, "net_sales": None,
                      "fwd_set": None, "rev_set": a, "net_set": None, "diff": None, "exc": set(), "hyg": set()}
    for e in exc:
        if e[1] in res:
            res[e[1]]["exc"].add(e[0])
    for v in res.values():
        if v["status"] == RSWF:
            v["exc"].discard(RSWF)

    # hygiene (one entry per finding row: (code, dataset, key, file row))
    hyg = []
    for ds in DATASET_ORDER:
        rows = sh[ds]
        for r in rows:
            if r["_period"] == month and ymd(r[PERIOD_COL[ds]]).strftime("%Y-%m") != month:
                hyg.append(("DATE_OUTSIDE_RECON_MONTH", ds, r[KEY[ds]], r["_row_no"], r["_period"]))
        if ds == "sales_forward":
            g = defaultdict(list)
            for r in rows:
                g[(r["packet_id"], r["order_status"].strip().upper())].append(r)
            hyg += [("DUPLICATE_KEY_SAME_STATUS", ds, r["packet_id"], r["_row_no"], r["_period"])
                    for v in g.values() if len(v) > 1 for r in v]
        if ds == "settlement_forward":       # no order_status column in settlement files -> key only
            g = defaultdict(list)
            for r in rows:
                g[r["order_release_id"]].append(r)
            hyg += [("DUPLICATE_KEY", ds, r["order_release_id"], r["_row_no"], r["_period"])
                    for v in g.values() if len(v) > 1 for r in v]
        g = defaultdict(list)
        for r in rows:
            g[(r["_period"], tuple((c, r[c]) for c in DATASETS[ds]["columns"]))].append(r)
        hyg += [("EXACT_DUPLICATE_ROW", ds, r[KEY[ds]], r["_row_no"], r["_period"])
                for v in g.values() for r in v[1:]]
    skeys = {r["order_release_id"] for r in sh["settlement_forward"]}
    for r in sh["sales_forward"]:
        k = r["packet_id"]
        for t in sh["settlement_forward"]:
            tk = t["order_release_id"]
            if k.lstrip("0") and tk.lstrip("0") == k.lstrip("0") and tk != k and k not in skeys:
                hyg.append(("KEY_DIFFERS_BY_LEADING_ZEROS", "sales_forward", k, r["_row_no"], r["_period"]))
    fwd_by = defaultdict(list)
    for r in sh["sales_forward"]:
        fwd_by[r["packet_id"]].append(r)
    for ds in ("sales_rt", "sales_rto"):
        cnt = defaultdict(int)
        for r in sh[ds]:
            cnt[r["packet_id"]] += 1
        for r in sh[ds]:
            k = r["packet_id"]
            for f in fwd_by.get(k, []):
                if D(f["shipment_value"]) != D(r["shipment_value"]):
                    hyg.append(("RETURN_AMOUNT_MISMATCH", ds, k, r["_row_no"], r["_period"]))
                if f["sku_id"].strip() != r["sku_id"].strip():
                    hyg.append(("RETURN_SKU_MISMATCH", ds, k, r["_row_no"], r["_period"]))
            if (ymd(r[RET_DATE[ds]]) - ymd(r["order_shipped_date"])).days > RETURN_WINDOW:
                hyg.append(("RETURN_OUTSIDE_WINDOW", ds, k, r["_row_no"], r["_period"]))
            if cnt[k] > 1:
                hyg.append(("MULTIPLE_RETURNS_PER_PACKET", ds, k, r["_row_no"], r["_period"]))
            if k not in fwd_by:
                hyg.append(("RETURN_WITHOUT_FORWARD_SALE", ds, k, r["_row_no"], r["_period"]))
    for r in sh["sales_rt"]:
        for o in sh["sales_rto"]:
            if o["packet_id"] == r["packet_id"]:
                hyg.append(("PACKET_IN_BOTH_RT_AND_RTO", "sales_rt", r["packet_id"], r["_row_no"], r["_period"]))
    for h in hyg:
        if h[2] in res:
            res[h[2]]["hyg"].add(h[0])

    # carry-forward / open items
    mend = date(mstart.year, mstart.month, calendar.monthrange(mstart.year, mstart.month)[1])
    lag_from = mend - timedelta(days=LAG - 1)
    sale_d = defaultdict(lambda: None)
    for r in sh["sales_forward"]:
        x = ymd(r["order_packed_date"])
        sale_d[r["packet_id"]] = max(filter(None, [sale_d[r["packet_id"]], x]))
    ret_d = defaultdict(lambda: None)
    for ds in ("sales_rt", "sales_rto"):
        for r in sh[ds]:
            x = ymd(r[RET_DATE[ds]])
            ret_d[r["packet_id"]] = max(filter(None, [ret_d[r["packet_id"]], x]))
    awaiting = set()
    for k, v in res.items():
        if v["status"] == RSWF:
            continue
        a = ((v["status"] == SNS and sale_d[k] and lag_from <= sale_d[k] <= mend) or
             (v["status"] in (SH, SS, RP) and ret_d[k] and lag_from <= ret_d[k] <= mend))
        v["awaiting"] = bool(a)
        if a:
            awaiting.add(k)
        o = open_items.get(k)
        if v["status"] == M:
            if o and o["is_open"]:
                o.update(is_open=False, closed_month=mstart, status=M)
        else:
            if o and o["is_open"]:
                o.update(status=v["status"], awaiting=bool(a), diff=v["diff"])
            else:
                open_items[k] = {"first_month": mstart, "status": v["status"], "is_open": True,
                                 "awaiting": bool(a), "diff": v["diff"], "closed_month": None}
        v["carried"] = k in carried
    for k in res:
        o = open_items.get(k)
        res[k]["open_since"] = o["first_month"] if o else None

    rows = list(res.values())
    by = defaultdict(lambda: {"n": 0, "ns": D(0), "nt": D(0), "diff": D(0), "mm": D(0)})
    for v in rows:
        b = by[v["status"]]
        b["n"] += 1
        b["ns"] += v["net_sales"] or 0
        b["nt"] += v["net_set"] or 0
        b["diff"] += v["diff"] or 0
        b["mm"] += 0 if v["status"] == M else abs(v["diff"] or 0)
    one_sided = {SNS, STNS, RSWF}
    matched = by[M]["n"] if M in by else 0
    compared = sum(b["n"] for s, b in by.items() if s not in one_sided)
    open_now = [k for k, o in open_items.items() if o["is_open"]]
    rk = {k for k, v in res.items() if v["status"] != RSWF}
    kpi = {
        "Total keys": len(rows), "Found on both sides": compared, "Matched": matched,
        "Match rate %": round(100.0 * matched / compared, 1) if compared else 0.0,
        "Net sales": sum(b["ns"] for b in by.values()), "Net settlement": sum(b["nt"] for b in by.values()),
        "Net difference": sum(b["diff"] for b in by.values()),
        "Unreconciled amount": sum(b["mm"] for b in by.values()),
        "Exceptions (reconciliation rows)": len(exc), "Hygiene findings": len(hyg),
        "Carried in": len(carried), "Closed (carried and now matched)": len(carried & {k for k in rk if res[k]["status"] == M}),
        "New open": len({k for k in rk if k not in carried and res[k]["status"] != M}),
        "Open items after run": len(open_now),
        "Awaiting (T+2) after run": len([k for k in open_now if open_items[k]["awaiting"]]),
    }
    return {"month": month, "rows": sorted(rows, key=lambda v: v["key"]), "by": dict(by), "kpi": kpi,
            "exc": exc, "hyg": hyg}


# --------------------------------------------------------------------------------------------- answer key
LABEL = {M: "Matched", SH: "Sales higher than settlement", SS: "Settlement higher than sales",
         RP: "Return processed but settlement not adjusted", SNS: "Sales not in settlement",
         STNS: "Settlement not in sales", RSWF: "Reverse settlement without forward settlement"}
STATUS_ORDER = [M, SH, SS, RP, SNS, STNS, RSWF]
EXC_LABEL = {
    "PACKET_ID_NOT_EQUAL_ORDER_RELEASE_ID": "Packet ID not equal to order release ID",
    "REVERSE_SETTLEMENT_WITHOUT_FORWARD": "Reverse settlement without forward settlement",
    "MULTIPLE_REVERSE_SETTLEMENTS": "Multiple reverse settlements for one order",
    "RETURN_IN_SALES_BUT_NO_REVERSE_SETTLEMENT": "Return in sales but no reverse settlement",
    "REVERSE_SETTLEMENT_BUT_NO_RETURN_IN_SALES": "Reverse settlement but no return in sales",
    "TAX_VARIANCE": "Tax variance",
    "DATE_OUTSIDE_RECON_MONTH": "Date outside reconciliation month",
    "DUPLICATE_KEY_SAME_STATUS": "Duplicate key with the same order status", "DUPLICATE_KEY": "Duplicate key",
    "EXACT_DUPLICATE_ROW": "Exact duplicate row", "KEY_DIFFERS_BY_LEADING_ZEROS": "Key differs only by leading zeros",
    "RETURN_AMOUNT_MISMATCH": "Return amount differs from sale amount",
    "RETURN_SKU_MISMATCH": "Returned SKU differs from ordered SKU",
    "RETURN_OUTSIDE_WINDOW": "Return outside the return window",
    "MULTIPLE_RETURNS_PER_PACKET": "Multiple return rows for one packet",
    "RETURN_WITHOUT_FORWARD_SALE": "Return without a forward sale",
    "PACKET_IN_BOTH_RT_AND_RTO": "Packet in both RT and RTO",
}
DS_LABEL = {d: DATASETS[d]["label"].split(" (")[0] for d in DATASET_ORDER}


def money(v):
    if v is None:
        return "–"
    v = D(v).normalize()
    s = f"{abs(v):,.3f}".rstrip("0").rstrip(".")
    return ("-" if v < 0 else "") + "₹" + s


def write_md(runs):
    L = ["# Sales vs Settlement — test data answer key", "",
         "Generated by `make_test_data.py`. Every number below is what the app must show.", "",
         "## How to run the test", "",
         "1. **Upload data** — upload the five files in `2019-01/` with data month **Jan 2019**, then the five in "
         "`2019-02/` with data month **Feb 2019** (dataset = file name).",
         "2. **Run reconciliation** — month **Jan 2019** (one month), platform **TESTMART**, tolerance **0.01**, "
         "return window **30**, tax tolerance **1.00**, standard column mapping. → compare with *Run 1* below.",
         "3. Run again for **Feb 2019**, same settings. → compare with *Run 2* (needs Run 1 first: it carries Jan's open items).",
         "",
         "> Run Jan before Feb, and only once each: open items are carried from run to run, so re-running Jan after "
         "Feb changes nothing, but a fresh database is the cleanest. Data months 2019 + platform TESTMART keep this "
         "apart from real data. Do not open the CSVs in Excel and save them (Excel drops the leading zeros of `0091001`).",
         "",
         "> **Completely blank column**: the test rows fill every standard column, so a fresh database gives 0. If your "
         "database already has extra columns from real uploads (e.g. `Settlement_on_2024_02_02`), each of those is blank "
         "for the test rows and is reported once per run — those findings are expected and are not counted below.",
         "", "## Scenarios", "", "| Key | What it tests | Jan run | Feb run | Notes |", "|---|---|---|---|---|"]
    for s in S:
        L.append(f"| `{s['key']}` | {s['title']} | {LABEL.get(s['expect'].get(JAN), '— (not in run)')} | "
                 f"{LABEL.get(s['expect'].get(FEB), '— (not in run)')} | {s['note']} |")
    for i, r in enumerate(runs, 1):
        mlabel = date.fromisoformat(r["month"] + "-01").strftime("%b %Y")
        L += ["", f"## Run {i} — {mlabel} · TESTMART", "", "### KPI cards", "", "| KPI | Expected |", "|---|---|"]
        for k, v in r["kpi"].items():
            L.append(f"| {k} | {money(v) if isinstance(v, D) else v} |")
        L += ["", "### Amounts by status", "", "| Status | Keys | Net sales | Net settlement | Difference | Unreconciled |",
              "|---|---:|---:|---:|---:|---:|"]
        for s in STATUS_ORDER:
            if s in r["by"]:
                b = r["by"][s]
                L.append(f"| {LABEL[s]} | {b['n']} | {money(b['ns'])} | {money(b['nt'])} | {money(b['diff'])} | {money(b['mm'])} |")
        L += ["", "### Key-level results", "",
              "| Key | Status | Fwd sales | Return | Net sales | Fwd settl. | Reverse | Net settl. | Difference | Exceptions | Hygiene flags | Carried | Awaiting |",
              "|---|---|---:|---|---:|---:|---:|---:|---:|---|---|---|---|"]
        for v in r["rows"]:
            ret = f"{v['ret_src']} {money(v['ret'])}" if v["ret_src"] and v["ret_src"] != "NONE" else "–"
            L.append(f"| `{v['key']}` | {LABEL[v['status']]} | {money(v['fwd_sales'])} | {ret} | {money(v['net_sales'])} | "
                     f"{money(v['fwd_set'])} | {money(v['rev_set'])} | {money(v['net_set'])} | {money(v['diff'])} | "
                     f"{'; '.join(sorted(EXC_LABEL[e] for e in v['exc'])) or '–'} | "
                     f"{'; '.join(sorted(EXC_LABEL[h] for h in v['hyg'])) or '–'} | "
                     f"{'yes' if v.get('carried') else ''} | {'yes' if v.get('awaiting') else ''} |")
        cnt = defaultdict(int)
        for e in r["exc"]:
            cnt[e[0]] += 1
        L += ["", "### Reconciliation exceptions (rows)", "", "| Exception | Count |", "|---|---:|"]
        L += [f"| {EXC_LABEL[c]} | {n} |" for c, n in sorted(cnt.items())] or ["| (none) | 0 |"]
        L += ["", "### Hygiene findings (rows)", "", "| Check | File | Key | File row | File month |", "|---|---|---|---:|---|"]
        for h in sorted(r["hyg"], key=lambda h: (h[0], DATASET_ORDER.index(h[1]), h[3])):
            L.append(f"| {EXC_LABEL[h[0]]} | {DS_LABEL[h[1]]} | `{h[2]}` | {h[3]} | {h[4]} |")
    with open(os.path.join(HERE, "EXPECTED_RESULTS.md"), "w", encoding="utf-8") as fh:
        fh.write("\n".join(L) + "\n")


def write_xlsx(runs):
    try:
        from openpyxl import Workbook
        from openpyxl.styles import Font
    except ImportError:
        print("openpyxl not installed: expected_results.xlsx skipped")
        return
    wb = Workbook()
    ws = wb.active
    ws.title = "Scenarios"
    ws.append(["Key", "What it tests", "Jan run", "Feb run", "Notes"])
    for s in S:
        ws.append([s["key"], s["title"], LABEL.get(s["expect"].get(JAN), ""), LABEL.get(s["expect"].get(FEB), ""), s["note"]])
    num = lambda v: float(v) if v is not None else None
    for i, r in enumerate(runs, 1):
        tag = date.fromisoformat(r["month"] + "-01").strftime("%b%Y")
        k = wb.create_sheet(f"Run{i} {tag} KPIs")
        k.append(["KPI", "Expected"])
        for a, v in r["kpi"].items():
            k.append([a, num(v) if isinstance(v, D) else v])
        k.append([])
        k.append(["Status", "Keys", "Net sales", "Net settlement", "Difference", "Unreconciled"])
        for s in STATUS_ORDER:
            if s in r["by"]:
                b = r["by"][s]
                k.append([LABEL[s], b["n"], num(b["ns"]), num(b["nt"]), num(b["diff"]), num(b["mm"])])
        w = wb.create_sheet(f"Run{i} {tag} Results")
        w.append(["Key", "Status", "Forward sales", "Return source", "Return amount", "Net sales", "Forward settlement",
                  "Reverse settlement", "Net settlement", "Difference", "Exceptions", "Hygiene flags", "Carried", "Awaiting"])
        for v in r["rows"]:
            w.append([v["key"], LABEL[v["status"]], num(v["fwd_sales"]), v["ret_src"], num(v["ret"]), num(v["net_sales"]),
                      num(v["fwd_set"]), num(v["rev_set"]), num(v["net_set"]), num(v["diff"]),
                      "; ".join(sorted(EXC_LABEL[e] for e in v["exc"])), "; ".join(sorted(EXC_LABEL[h] for h in v["hyg"])),
                      "yes" if v.get("carried") else "", "yes" if v.get("awaiting") else ""])
        h = wb.create_sheet(f"Run{i} {tag} Hygiene")
        h.append(["Check", "File", "Key", "File row", "File month"])
        for x in sorted(r["hyg"], key=lambda h: (h[0], DATASET_ORDER.index(h[1]), h[3])):
            h.append([EXC_LABEL[x[0]], DS_LABEL[x[1]], x[2], x[3], x[4]])
    for s in wb.worksheets:
        for c in s[1]:
            c.font = Font(bold=True)
        for col in s.columns:
            s.column_dimensions[col[0].column_letter].width = min(max(len(str(c.value or "")) for c in col) + 2, 60)
    wb.save(os.path.join(HERE, "expected_results.xlsx"))


def main():
    files = build_files()
    data = load(files)
    open_items = {}
    runs = [run(data, JAN, open_items), run(data, FEB, open_items)]
    # the reference engine must agree with the hand-written expectation of every scenario
    bad = []
    for r in runs:
        got = {v["key"]: v["status"] for v in r["rows"]}
        want = {s["key"].upper(): s["expect"][r["month"]] for s in S if r["month"] in s["expect"]}
        for k in set(got) | set(want):
            if got.get(k) != want.get(k):
                bad.append(f"{r['month']} {k}: engine {got.get(k)} vs expected {want.get(k)}")
    if bad:
        sys.exit("Scenario expectation mismatch:\n  " + "\n  ".join(sorted(bad)))
    write_md(runs)
    write_xlsx(runs)
    for r in runs:
        print(r["month"], {k: (str(v) if isinstance(v, D) else v) for k, v in r["kpi"].items()})


if __name__ == "__main__":
    main()
