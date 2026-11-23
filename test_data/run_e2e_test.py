"""End-to-end check: load the test data into a SEPARATE database through the real API, run Jan + Feb,
and compare every number with the answer key produced by make_test_data.py.

    python run_e2e_test.py            (database: reconone_test on the same server as ar_recon)

The database must exist and should be empty (create it with: CREATE DATABASE reconone_test).
"""
import os
import sys
from decimal import Decimal as D

HERE = os.path.dirname(os.path.abspath(__file__))
BACKEND = os.path.join(HERE, "..", "backend")
sys.path.insert(0, BACKEND)
sys.path.insert(0, HERE)

import config  # noqa: E402  (sets up libpq first)

TEST_DB = os.getenv("RECONONE_TEST_DB", "reconone_test")
assert TEST_DB != "ar_recon", "never run the test against the real database"
config.DATABASE_URL = config.DATABASE_URL.rsplit("/", 1)[0] + "/" + TEST_DB

import db  # noqa: E402
import make_test_data as mtd  # noqa: E402
import server  # noqa: E402
from schema import DATASET_ORDER  # noqa: E402

LABEL = mtd.LABEL
fails = []


def check(what, got, want):
    if isinstance(want, (D, float)) or isinstance(got, (D, float)):
        ok = got is not None and want is not None and abs(D(str(got)) - D(str(want))) < D("0.0005")
        ok = ok or (got is None and want is None)
    else:
        ok = got == want
    if not ok:
        fails.append(f"{what}: app {got!r} vs expected {want!r}")
    return ok


def main():
    db.ensure_schema()
    c = server.app.test_client()
    # 1. upload
    for m in (mtd.JAN, mtd.FEB):
        for ds in DATASET_ORDER:
            path = os.path.join(HERE, m, f"{ds}.csv")
            with open(path, "rb") as fh:
                r = c.post("/api/upload", data={"dataset": ds, "period": m, "mode": "replace",
                                                 "file": (fh, f"{ds}.csv")}, content_type="multipart/form-data")
            j = r.get_json()
            assert r.status_code == 200, (m, ds, j)
            print(f"uploaded {m} {ds}: {j.get('rows_loaded')} rows")
    # 2. mapping = what the Run screen pre-selects
    setup = c.get("/api/run/setup").get_json()
    mapping = {d["name"]: {f["role"]: f["selected"] for f in d["fields"]} for s in setup["sides"] for d in s["datasets"]}
    # 3. expected answer (same reference engine as the answer key)
    files = mtd.build_files()
    data = mtd.load(files)
    oi = {}
    expected = [mtd.run(data, mtd.JAN, oi), mtd.run(data, mtd.FEB, oi)]
    # 4. run + compare
    for exp in expected:
        m = exp["month"]
        r = c.post("/api/run", json={"month": m, "tolerance": 0.01, "return_window": 30, "commission_tol": 1.0,
                                     "platform": mtd.PLATFORM, "mapping": mapping})
        j = r.get_json()
        assert r.status_code == 200, (m, j)
        rid = j["run_id"]
        s = c.get(f"/api/summary/{rid}").get_json()
        k, ek = s["kpi"], exp["kpi"]
        print(f"\n=== {m} run #{rid} ===")
        pairs = [("total_keys", "Total keys"), ("compared", "Found on both sides"), ("matched", "Matched"),
                 ("match_rate", "Match rate %"), ("net_sales", "Net sales"), ("net_settlement", "Net settlement"),
                 ("difference", "Net difference"), ("mismatched_amount", "Unreconciled amount"),
                 ("exceptions", "Exceptions (reconciliation rows)"), ("carried_in", "Carried in"),
                 ("closed_prior", "Closed (carried and now matched)"), ("open_items", "Open items after run"),
                 ("awaiting", "Awaiting (T+2) after run")]
        for a, b in pairs:
            ok = check(f"{m} KPI {b}", k[a], ek[b])
            print(f"  {'OK ' if ok else 'XX '} {b:34} app={k[a]!s:>12}  expected={ek[b]!s:>12}")
        # hygiene: the app also counts blank columns that exist in the DB but not in the test rows
        blank = sum(ch["records_failed"] or 0 for ch in s["checks"] if ch["check_code"] == "BLANK_COLUMN")
        ok = check(f"{m} KPI Hygiene findings (excl. blank columns)", k["hygiene"] - blank, ek["Hygiene findings"])
        print(f"  {'OK ' if ok else 'XX '} {'Hygiene findings':34} app={k['hygiene']:>12}  expected={ek['Hygiene findings']:>12}"
              f"  (blank-column findings: {blank})")
        # statuses
        for st in s["statuses"]:
            b = exp["by"].get(st["code"])
            check(f"{m} status {st['label']} keys", st["n"], b and b["n"])
            if b:
                check(f"{m} status {st['label']} net sales", st["net_sales"], b["ns"])
                check(f"{m} status {st['label']} net settlement", st["net_settlement"], b["nt"])
                check(f"{m} status {st['label']} difference", st["difference"], b["diff"])
        missing = set(exp["by"]) - {st["code"] for st in s["statuses"]}
        for st in missing:
            fails.append(f"{m} status {st} missing in app")
        # key level
        res = c.get(f"/api/results/{rid}?size=500").get_json()["rows"]
        got = {x["key"]: x for x in res}
        want = {v["key"]: v for v in exp["rows"]}
        for key in sorted(set(got) | set(want)):
            g, w = got.get(key), want.get(key)
            if not g or not w:
                fails.append(f"{m} key {key}: {'missing in app' if not g else 'unexpected in app'}")
                continue
            check(f"{m} {key} status", g["status"], w["status"])
            for a, b in (("forward_sales", "fwd_sales"), ("net_sales", "net_sales"), ("forward_settlement", "fwd_set"),
                         ("reverse_settlement", "rev_set"), ("net_settlement", "net_set"), ("difference", "diff")):
                check(f"{m} {key} {a}", g[a], w[b])
            check(f"{m} {key} exceptions", sorted(g["exceptions"]), sorted(mtd.EXC_LABEL[e] for e in w["exc"]))
            hyg = sorted(h for h in g["hygiene_flags"] if h != "Completely blank column")
            check(f"{m} {key} hygiene flags", hyg, sorted(mtd.EXC_LABEL[h] for h in w["hyg"]))
            check(f"{m} {key} carried", bool(g.get("carried")), bool(w.get("carried")))
        print(f"  key-level rows compared: {len(want)}")
        # hygiene findings per check
        hc = {}
        for ch in s["checks"]:
            if ch["category"] == "HYGIENE" and ch["check_code"] != "BLANK_COLUMN":
                hc[ch["check_code"]] = hc.get(ch["check_code"], 0) + (ch["records_failed"] or 0)
        eh = {}
        for h in exp["hyg"]:
            eh[h[0]] = eh.get(h[0], 0) + 1
        for code in sorted(set(hc) | set(eh)):
            if hc.get(code, 0) or eh.get(code, 0):
                check(f"{m} hygiene {code} rows", hc.get(code, 0), eh.get(code, 0))
    print("\n" + ("ALL CHECKS PASSED" if not fails else f"{len(fails)} MISMATCH(ES):\n  " + "\n  ".join(fails)))
    return 1 if fails else 0


if __name__ == "__main__":
    sys.exit(main())
