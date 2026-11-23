"""ReconOne backend - Sales vs Settlement.

Flask JSON API + the ReconOne UI (../frontend/index.html) served from the same origin.
All data lives in the PostgreSQL database set in config.py (default: reconone) - open it in pgAdmin.

Run:  python server.py   ->  http://127.0.0.1:8000

API (all JSON unless noted)
    GET    /api/health
    GET    /api/data                       datasets, rows per data month, upload log, data-month profiles
    POST   /api/upload                     multipart: dataset, period (YYYY-MM), mode (replace|append), sheet, file
    DELETE /api/data/<dataset>?period=     clear one month of a dataset (no period = whole dataset)
    GET    /api/run/setup                  column-mapping dropdowns, platforms, loaded months, defaults
    GET    /api/values/<dataset>?column=   distinct platform values of a column
    POST   /api/run                        {month, month_to, tolerance, return_window, commission_tol, platform, mapping}
    GET    /api/runs                       run history (incl. failed runs)
    GET    /api/summary/<run_id>           KPIs, amounts by status, exceptions, hygiene check log, column mapping
    GET    /api/results/<run_id>           ?status=&exc=&q=&page=&size=
    GET    /api/status-trend               match rate per run
    GET    /api/row/<dataset>/<row_no>     one source row of an uploaded file
    GET    /api/open-items                 ?platform=&show=open|closed
    GET    /api/data-check                 ?period=
    POST   /api/data-check/refresh
    GET    /api/download/<run_id>          (xlsx) full reconciliation workbook
    GET    /api/download/<run_id>/results  (xlsx) ?status=&exc=&q=
    GET    /api/open-items/download        (xlsx) ?platform=&show=
"""
import logging
import math
import os
import re
import tempfile
from datetime import date, datetime, timedelta
from decimal import Decimal

from flask import Flask, abort, jsonify, request, send_file, send_from_directory

import config  # noqa: F401 (imported first: sets up libpq on Windows before psycopg loads)
from psycopg import sql

import carry
import db
import export
import ingest
import mapping as colmap
import profile_data
import recon
from schema import DATASET_ORDER, DATASETS

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger("reconone")

HERE = os.path.dirname(os.path.abspath(__file__))
FRONTEND = os.path.normpath(os.path.join(HERE, "..", "frontend"))
EXPORT_DIR = os.path.join(HERE, "exports")

app = Flask(__name__, static_folder=None)
app.config["MAX_CONTENT_LENGTH"] = int(os.getenv("RECONONE_MAX_UPLOAD_MB", "500")) * 1024 * 1024

STATUS_COLORS = {
    "MATCHED": "#159a63",
    "SALES_HIGHER_THAN_SETTLEMENT": "#cc3340",
    "SETTLEMENT_HIGHER_THAN_SALES": "#c47f04",
    "RETURN_PROCESSED_SETTLEMENT_NOT_ADJUSTED": "#7b61ff",
    "SALES_NOT_IN_SETTLEMENT": "#1E5EFF",
    "SETTLEMENT_NOT_IN_SALES": "#0FA3A3",
    "REVERSE_SETTLEMENT_WITHOUT_FORWARD": "#e5308a",
    "UNMATCHED": "#cc3340",
}
ONE_SIDED = {"SALES_NOT_IN_SETTLEMENT", "SETTLEMENT_NOT_IN_SALES", "REVERSE_SETTLEMENT_WITHOUT_FORWARD"}


# --------------------------------------------------------------------------- helpers
def J(v):
    """JSON-safe value (Decimal / date / nested)."""
    if isinstance(v, Decimal):
        return float(v)
    if isinstance(v, (date, datetime)):
        return v.isoformat()
    if isinstance(v, dict):
        return {k: J(x) for k, x in v.items()}
    if isinstance(v, (list, tuple)):
        return [J(x) for x in v]
    return v


def err(msg, code=400, **extra):
    return jsonify({"ok": False, "error": msg, **extra}), code


@app.errorhandler(404)
def _404(e):
    if request.path.startswith("/api/"):
        return err("Not found", 404)
    return e


@app.errorhandler(Exception)
def _any(e):
    from werkzeug.exceptions import HTTPException
    if isinstance(e, HTTPException):
        return e
    log.exception("Unhandled error on %s", request.path)
    return err(str(e), 500)


@app.after_request
def _cors(resp):
    # lets the UI also be opened from another origin (e.g. a dev server); harmless on same origin
    resp.headers["Access-Control-Allow-Origin"] = "*"
    resp.headers["Access-Control-Allow-Methods"] = "GET, POST, DELETE, OPTIONS"
    resp.headers["Access-Control-Allow-Headers"] = "Content-Type"
    return resp


def _run_row(cur, run_id):
    cur.execute("SELECT * FROM recon_runs WHERE run_id = %s", (run_id,))
    r = cur.fetchone()
    if not r:
        abort(404)
    return r


def _run_public(r):
    return {
        "run_id": r["run_id"],
        "created_at": J(r["created_at"]),
        "period": carry.month_span(r),
        "recon_month": J(r.get("recon_month")),
        "recon_month_to": J(r.get("recon_month_to") or r.get("recon_month")),
        "platform": r.get("platform_filter") or "",
        "status": r.get("status"),
        "message": r.get("message"),
        "tolerance": J(r.get("tolerance")),
        "return_window": r.get("return_window"),
        "commission_tol": J(r.get("commission_tol")),
        "commission_pct": J(r.get("commission_pct")),
        "logistics_pct": J(r.get("logistics_pct")),
        "logistics_gst_rate": J(r.get("logistics_gst_rate")),
        "tcs_rate": J(r.get("tcs_rate")), "tds_rate": J(r.get("tds_rate")), "pg_rate": J(r.get("pg_rate")),
        "matched": r.get("matched"),
        "unmatched": r.get("unmatched"),
        "sales_only": r.get("sales_only"),
        "settlement_only": r.get("settlement_only"),
        "exception_count": r.get("exception_count"),
        "hygiene_count": r.get("hygiene_count"),
    }


def _send_xlsx(buf, name):
    """Send the workbook AND keep a copy in backend/exports."""
    os.makedirs(EXPORT_DIR, exist_ok=True)
    with open(os.path.join(EXPORT_DIR, name), "wb") as f:
        f.write(buf.getbuffer())
    buf.seek(0)
    return send_file(buf, as_attachment=True, download_name=name,
                     mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")


def _platform_values(cur, dataset, column, limit=200):
    if dataset not in DATASETS or column not in db.existing_columns(cur, dataset):
        return []
    cur.execute(sql.SQL("""SELECT min(btrim({c})) AS p FROM {t} WHERE {c} IS NOT NULL AND btrim({c}) <> ''
                           GROUP BY upper(btrim({c})) ORDER BY 1 LIMIT %s""").format(
        c=db.q(column), t=db.q(dataset)), (limit,))
    return [r["p"] for r in cur.fetchall()]


def _profile_public(p):
    return {**J({k: v for k, v in p.items() if k != "flags"}),
            "flags": [{"level": lv, "message": m} for lv, m in p["flags"]]}


# --------------------------------------------------------------------------- UI
@app.route("/")
def index():
    return send_from_directory(FRONTEND, "index.html")


@app.route("/api/health")
def health():
    with db.cursor(commit=False) as cur:
        cur.execute("SELECT current_database() AS db, version() AS v")
        r = cur.fetchone()
    return jsonify({"ok": True, "database": r["db"], "postgres": r["v"].split(",")[0]})


# --------------------------------------------------------------------------- data (upload)
@app.route("/api/data")
def data():
    counts, upload_log = db.table_counts()
    with db.cursor(commit=False) as cur:
        profiles = profile_data.load(cur)
    periods = sorted({p for d in counts.values() for p in d["periods"]}, reverse=True)
    return jsonify({
        "datasets": [{"name": d, "label": DATASETS[d]["label"], **counts[d]} for d in DATASET_ORDER],
        "periods": periods,
        "upload_log": [J(dict(x)) for x in upload_log],
        "profiles": {p: {d: _profile_public(v) for d, v in by.items()} for p, by in profiles.items()},
        "default_period": date.today().strftime("%Y-%m"),
    })


@app.route("/api/upload", methods=["POST"])
def upload():
    dataset = request.form.get("dataset")
    mode = request.form.get("mode", "replace")
    sheet = (request.form.get("sheet") or "").strip() or None
    period = (request.form.get("period") or "").strip()
    f = request.files.get("file")
    if not re.fullmatch(r"\d{4}-\d{2}", period):
        return err("Choose the data month of the file.")
    if dataset not in DATASETS:
        return err("Choose a dataset.")
    if not f or not f.filename:
        return err("Choose a file.")
    if mode not in ("replace", "append"):
        return err("Mode must be replace or append.")
    suffix = os.path.splitext(f.filename)[1].lower()
    if suffix not in (".xlsx", ".xlsm", ".xls", ".csv", ".txt"):
        return err("Only .xlsx / .xls / .csv files are accepted.")
    with tempfile.NamedTemporaryFile(suffix=suffix, delete=False) as tmp:
        f.save(tmp.name)
        path = tmp.name
    try:
        if sheet and sheet.isdigit():
            sheet = int(sheet)
        res = ingest.load_file(dataset, path, f.filename, sheet=sheet, mode=mode, period=period)
    except Exception as e:  # noqa: BLE001
        log.exception("Upload failed")
        return err(f"Load failed: {e}")
    finally:
        os.unlink(path)
    with db.cursor(commit=False) as cur:
        prof = profile_data.load(cur).get(period, {}).get(dataset)
    return jsonify({"ok": True, "dataset": dataset, "label": DATASETS[dataset]["label"], "period": period,
                    "mode": mode, "file": f.filename, **J(res),
                    "profile": _profile_public(prof) if prof else None})


@app.route("/api/data/<dataset>", methods=["DELETE"])
def clear(dataset):
    if dataset not in DATASETS:
        return err("Unknown dataset", 404)
    period = (request.args.get("period") or "").strip()
    with db.cursor() as cur:
        if period == "(no month)":
            cur.execute(sql.SQL("DELETE FROM {} WHERE {} IS NULL OR {} = ''").format(
                db.q(dataset), db.q("_period"), db.q("_period")))
        elif period:
            db.delete_period(cur, dataset, period)
            profile_data.refresh(cur, dataset, period)
        else:
            db.truncate(cur, dataset)
            cur.execute("DELETE FROM data_profile WHERE dataset = %s", (dataset,))
    return jsonify({"ok": True})


# --------------------------------------------------------------------------- run
@app.route("/api/run/setup")
def run_setup():
    with db.cursor(commit=False) as cur:
        sides = colmap.suggest(cur)
        platform_col = next((f["selected"] for side in sides for d in side["datasets"]
                             if d["name"] == "settlement_forward" for f in d["fields"] if f["role"] == "platform"), "")
        platforms = _platform_values(cur, "settlement_forward", platform_col) if platform_col else []
    loaded = db.loaded_periods()
    return jsonify({
        "sides": [{"side": s["side"], "datasets": [{
            "name": d["name"], "label": d["label"], "rows": d["rows"],
            "columns": [{"value": c, "header": h} for c, h in d["columns"]],
            "fields": [{"role": f["role"], "label": f["label"], "required": f["required"], "help": f.get("help", ""),
                        "advanced": f.get("advanced", False), "group": f.get("group"),
                        "selected": f["selected"]} for f in d["fields"]],
        } for d in s["datasets"]]} for s in sides],
        "platforms": platforms,
        "fee_groups": [{"code": c, "label": l} for c, l in colmap.FEE_GROUPS],
        "loaded_periods": loaded,
        "defaults": {"month": loaded[0] if loaded else date.today().strftime("%Y-%m"), "month_to": "",
                     "tolerance": config.DEFAULT_TOLERANCE, "return_window": config.DEFAULT_RETURN_WINDOW_DAYS,
                     "commission_tol": config.DEFAULT_COMMISSION_TOLERANCE,
                     "commission_pct": config.DEFAULT_COMMISSION_PCT, "logistics_pct": config.DEFAULT_LOGISTICS_PCT,
                     "logistics_gst_rate": config.LOGISTICS_GST_RATE, "tcs_rate": config.TCS_RATE,
                     "tds_rate": config.TDS_RATE, "pg_rate": config.PG_FEE_RATE},
    })


@app.route("/api/values/<dataset>")
def column_values(dataset):
    with db.cursor(commit=False) as cur:
        return jsonify(_platform_values(cur, dataset, request.args.get("column") or ""))


def _pct(v, label):
    """Allowed % from the Run screen: blank = check not run, otherwise 0..100."""
    if v in (None, "") or (isinstance(v, str) and not v.strip()):
        return None
    x = float(v)
    if not 0 <= x <= 100:
        raise ValueError(f"{label} must be between 0 and 100")
    return x


@app.route("/api/run", methods=["POST"])
def run():
    p = request.get_json(silent=True) or {}
    try:
        month = datetime.strptime(p.get("month") or "", "%Y-%m").date().replace(day=1)
        mt = (p.get("month_to") or "").strip()
        month_to = datetime.strptime(mt, "%Y-%m").date().replace(day=1) if mt else month
        if month_to < month:
            raise ValueError("'To month' must be the same as or after 'From month'")
        if (month_to.year - month.year) * 12 + (month_to.month - month.month) > 1:
            raise ValueError("Choose at most 2 consecutive months (e.g. Feb 2024 - Mar 2024)")
        tolerance = float(p.get("tolerance") if p.get("tolerance") not in (None, "") else config.DEFAULT_TOLERANCE)
        window = int(p.get("return_window") if p.get("return_window") not in (None, "") else config.DEFAULT_RETURN_WINDOW_DAYS)
        comm_tol = float(p.get("commission_tol") if p.get("commission_tol") not in (None, "") else config.DEFAULT_COMMISSION_TOLERANCE)
        comm_pct, log_pct = (_pct(p.get(k), lbl) for k, lbl in (("commission_pct", "Allowed commission %"),
                                                                 ("logistics_pct", "Allowed logistics %")))
        rates = {k: _pct(p.get(f), lbl) for k, f, lbl in (("gst", "logistics_gst_rate", "GST rate on logistics"),
                                                          ("tcs", "tcs_rate", "TCS rate"), ("tds", "tds_rate", "TDS rate"),
                                                          ("pg", "pg_rate", "Payment gateway fee rate"))}
        platform = (p.get("platform") or "").strip()
    except Exception as e:  # noqa: BLE001
        return err(f"Check the parameters: {e}")

    raw = {d: {k: (v or "") for k, v in (p.get("mapping") or {}).get(d, {}).items()} for d in DATASET_ORDER}
    with db.cursor() as cur:
        m, errors = colmap.validate(cur, raw, platform)
        if not errors:
            colmap.save(cur, m)          # remembered as the default for the next run
    if errors:
        return err("Fix the column mapping", errors=errors)
    try:
        run_id = recon.run_reconciliation(month, tolerance, window, comm_tol, platform, m, month_to,
                                          commission_pct=comm_pct, logistics_pct=log_pct,
                                          rates=rates)
    except Exception as e:  # noqa: BLE001
        log.exception("Run failed")
        return err(f"Run failed: {e}", 500)
    return jsonify({"ok": True, "run_id": run_id})


@app.route("/api/runs")
def runs():
    with db.cursor(commit=False) as cur:
        cur.execute("SELECT * FROM recon_runs ORDER BY run_id DESC LIMIT 100")
        return jsonify([_run_public(r) for r in cur.fetchall()])


# --------------------------------------------------------------------------- results
@app.route("/api/summary/<int:run_id>")
def summary(run_id):
    with db.cursor(commit=False) as cur:
        r = _run_row(cur, run_id)
        by_status, grand = export.amount_totals(cur, run_id)
        cur.execute("""SELECT category, exception_type, count(*) AS n FROM recon_exceptions
                       WHERE run_id = %s GROUP BY 1,2 ORDER BY 1, 3 DESC""", (run_id,))
        exc = cur.fetchall()
        hyg = export.check_log(cur, run_id, "HYGIENE")
        rec = export.check_log(cur, run_id, "RECONCILIATION")
        cur.execute("""SELECT count(*) FILTER (WHERE is_open) AS open_items,
                              count(*) FILTER (WHERE is_open AND awaiting_settlement) AS awaiting
                       FROM open_items WHERE platform = %s""", (r.get("platform_filter") or "",))
        oi = cur.fetchone()
        cur.execute("""SELECT exception_type, count(*) AS n, count(DISTINCT coalesce(order_release_id, packet_id)) AS keys,
                              coalesce(sum(difference), 0) AS amount
                       FROM recon_exceptions WHERE run_id = %s AND category = 'RECONCILIATION'
                         AND exception_type = ANY(%s) GROUP BY 1""", (run_id, list(recon.FEE_CHECKS)))
        fee_rows = {x["exception_type"]: x for x in cur.fetchall()}
        # a check run on both settlement files: FAIL wins over PASS wins over NOT RUN
        rank = {"FAIL": 2, "PASS": 1, "NOT RUN": 0}
        fee_status, fee_notes = {}, {}
        for c in rec:
            code = c["check_code"]
            if code not in recon.FEE_CHECKS:
                continue
            if rank.get(c["status"], 0) >= rank.get(fee_status.get(code), -1):
                fee_status[code] = c["status"]
            if c["note"]:
                fee_notes.setdefault(code, c["note"])
        cur.execute("""SELECT payment_type, utr, settlement_date, orders, rows, amount FROM recon_utr
                       WHERE run_id = %s ORDER BY amount DESC""", (run_id,))
        utr_rows = cur.fetchall()
    m = colmap.Mapping.from_json(r.get("column_mapping"))
    utr = {"n": len(utr_rows), "amount": J(sum((x["amount"] or 0) for x in utr_rows)),
           "rows": [J(dict(x)) for x in utr_rows[:200]]}
    fees = [{"code": code, "label": export.label(code), "description": recon.EXCEPTIONS[code][1],
             "status": fee_status.get(code, "NOT RUN"), "note": fee_notes.get(code, ""),
             "n": fee_rows[code]["n"] if code in fee_rows else 0,
             "keys": fee_rows[code]["keys"] if code in fee_rows else 0,
             "amount": J(fee_rows[code]["amount"]) if code in fee_rows else 0}
            for code in recon.FEE_CHECKS]
    n_matched = next((s["n"] for s in by_status if s["status"] == "MATCHED"), 0)
    compared = sum(s["n"] for s in by_status if s["status"] not in ONE_SIDED)
    return jsonify({
        "run": _run_public(r),
        "kpi": {
            "total_keys": grand["n"], "compared": compared, "matched": n_matched,
            "match_rate": round(100.0 * n_matched / compared, 1) if compared else 0.0,
            "net_sales": J(grand["net_sales"]), "net_settlement": J(grand["net_settlement"]),
            "difference": J(grand["difference"]), "matched_amount": J(grand["matched_amount"]),
            "mismatched_amount": J(grand["mismatched_amount"]),
            "exceptions": r.get("exception_count") or 0, "hygiene": r.get("hygiene_count") or 0,
            "carried_in": r.get("carried_in"), "closed_prior": r.get("closed_prior"), "new_open": r.get("new_open"),
            "open_items": oi["open_items"], "awaiting": oi["awaiting"], "lag_days": config.SETTLEMENT_LAG_DAYS,
        },
        "statuses": [{
            "code": s["status"], "label": export.label(s["status"]),
            "description": recon.STATUSES.get(s["status"], ("", ""))[1],
            "color": STATUS_COLORS.get(s["status"], "#888"), "n": s["n"],
            "net_sales": J(s["net_sales"]), "net_settlement": J(s["net_settlement"]),
            "difference": J(s["difference"]), "mismatched_amount": J(s["mismatched_amount"]),
        } for s in by_status],
        "exceptions": [{"category": e["category"], "code": e["exception_type"],
                        "label": export.label(e["exception_type"]), "n": e["n"]} for e in exc],
        "checks": [J({k: c[k] for k in ("category", "check_code", "check_name", "description", "dataset",
                                        "columns_checked", "records_checked", "records_failed", "status", "note")})
                   for c in list(hyg) + list(rec)],
        "mapping": colmap.describe(m) if m else [],
        "fees": fees,
        "utr": utr,
    })


@app.route("/api/results/<int:run_id>")
def results(run_id):
    status = request.args.get("status") or ""
    exc = request.args.get("exc") or ""
    search = (request.args.get("q") or "").strip()
    size = min(max(int(request.args.get("size", 50)), 10), 500)
    page = max(int(request.args.get("page", 1)), 1)
    where, params = ["run_id = %s"], [run_id]
    sf, sp = export.status_filter(status)
    if sf:
        where.append(sf)
        params += sp
    ef, ep = export.exception_filter(exc)
    if ef:
        where.append(ef)
        params += ep
    if search:
        where.append("(packet_id ILIKE %s OR order_release_id ILIKE %s)")
        params += [f"%{search}%", f"%{search}%"]
    w = " AND ".join(where)
    with db.cursor(commit=False) as cur:
        _run_row(cur, run_id)
        cur.execute(f"SELECT count(*) AS n FROM recon_results WHERE {w}", params)
        total = cur.fetchone()["n"]
        pages = max(math.ceil(total / size), 1)
        page = min(page, pages)
        cur.execute(f"""SELECT packet_id, order_release_id, status, forward_sales, return_amount, return_source,
                               net_sales, forward_settlement, reverse_settlement, net_settlement, difference,
                               exceptions, hygiene_flags, carried, first_month
                        FROM recon_results WHERE {w}
                        ORDER BY (status = 'MATCHED'), abs(difference) DESC NULLS LAST, packet_id, order_release_id
                        LIMIT %s OFFSET %s""", params + [size, (page - 1) * size])
        rows = cur.fetchall()
        # source rows of the keys on this page (for the drill-down)
        keys = [x["packet_id"] or x["order_release_id"] for x in rows]
        src = {}
        if keys:
            m = colmap.default_mapping(cur)
            cur.execute("SELECT column_mapping FROM recon_runs WHERE run_id = %s", (run_id,))
            mj = cur.fetchone()["column_mapping"]
            m = colmap.Mapping.from_json(mj) or m
            for d in DATASET_ORDER:
                k = m.col(d, "key")
                if k is None:
                    continue
                cur.execute(sql.SQL("SELECT btrim({k}) AS k, array_agg({rn} ORDER BY {rn}) AS r FROM {t} "
                                    "WHERE btrim({k}) = ANY(%s) GROUP BY 1").format(
                    k=k, rn=db.q("_row_no"), t=db.q(d)), (keys,))
                for x in cur.fetchall():
                    src.setdefault(x["k"], {})[d] = x["r"][:20]
    out = []
    for x in rows:
        d = J(dict(x))
        d["key"] = x["packet_id"] or x["order_release_id"]
        d["status_label"] = export.label(x["status"])
        d["status_color"] = STATUS_COLORS.get(x["status"], "#888")
        d["exceptions"] = [export.label(c) for c in (x["exceptions"] or [])]
        d["hygiene_flags"] = [export.label(c) for c in (x["hygiene_flags"] or [])]
        d["source_rows"] = src.get(d["key"], {})
        out.append(d)
    return jsonify({"total": total, "page": page, "pages": pages, "size": size, "rows": out})


@app.route("/api/fee-rows/<int:run_id>")
def fee_rows(run_id):
    """Row-level findings of one fee check (charged / allowed / excess per row), for the dashboard drill-down."""
    code = request.args.get("code") or ""
    if code not in recon.FEE_CHECKS:
        return err("Unknown fee check")
    search = (request.args.get("q") or "").strip()
    size = min(max(int(request.args.get("size", 25)), 10), 500)
    page = max(int(request.args.get("page", 1)), 1)
    where, params = "run_id = %s AND exception_type = %s", [run_id, code]
    if search:
        where += " AND (order_release_id ILIKE %s OR packet_id ILIKE %s)"
        params += [f"%{search}%", f"%{search}%"]
    with db.cursor(commit=False) as cur:
        _run_row(cur, run_id)
        cur.execute(f"""SELECT count(*) AS n, count(DISTINCT order_release_id) AS keys,
                               coalesce(sum(difference), 0) AS amount FROM recon_exceptions WHERE {where}""", params)
        t = cur.fetchone()
        pages = max(math.ceil(t["n"] / size), 1)
        page = min(page, pages)
        cur.execute(f"""SELECT dataset, row_no, packet_id, order_release_id, amount_1, amount_2, difference, description
                        FROM recon_exceptions WHERE {where}
                        ORDER BY difference DESC NULLS LAST, order_release_id, row_no
                        LIMIT %s OFFSET %s""", params + [size, (page - 1) * size])
        rows = cur.fetchall()
    return jsonify({"code": code, "label": export.label(code), "description": recon.EXCEPTIONS[code][1],
                    "total": t["n"], "keys": t["keys"], "amount": J(t["amount"]), "page": page, "pages": pages,
                    "rows": [{**J(dict(x)), "dataset_label": export.dataset_label(x["dataset"])} for x in rows]})


@app.route("/api/status-trend")
def status_trend():
    with db.cursor(commit=False) as cur:
        cur.execute("""SELECT r.*, coalesce(t.diff, 0) AS diff FROM recon_runs r
                       LEFT JOIN (SELECT run_id, sum(abs(difference)) FILTER (WHERE status <> 'MATCHED') AS diff
                                  FROM recon_results GROUP BY run_id) t ON t.run_id = r.run_id
                       WHERE r.status = 'DONE' ORDER BY r.run_id DESC LIMIT 12""")
        rows = list(reversed(cur.fetchall()))
    out = []
    for r in rows:
        compared = (r.get("matched") or 0) + (r.get("unmatched") or 0)
        out.append({"run_id": r["run_id"], "label": f"#{r['run_id']} {carry.month_span(r, '%b %y')}",
                    "match_rate": round(100.0 * (r.get("matched") or 0) / compared, 1) if compared else 0,
                    "mismatched_amount": J(r["diff"])})
    return jsonify(out)


@app.route("/api/row/<dataset>/<int:row_no>")
def source_row(dataset, row_no):
    if dataset not in DATASETS:
        return err("Unknown dataset", 404)
    with db.cursor(commit=False) as cur:
        cur.execute(sql.SQL("SELECT * FROM {} WHERE {} = %s").format(db.q(dataset), db.q("_row_no")), (row_no,))
        rows = cur.fetchall()
    if not rows:
        return err("Row not found", 404)
    return jsonify({"dataset": dataset, "label": DATASETS[dataset]["label"],
                    "rows": [{k: J(v) for k, v in r.items() if v not in (None, "")} for r in rows]})


# --------------------------------------------------------------------------- open items / data check
def _open_items(platform, show):
    q, params = carry.open_items_query(platform, only_open=(show != "closed"))
    with db.cursor(commit=False) as cur:
        cur.execute(q, params)
        rows = cur.fetchall()
    return [r for r in rows if not r["is_open"]] if show == "closed" else rows


@app.route("/api/open-items")
def open_items():
    platform = request.args.get("platform", "")
    show = request.args.get("show", "open")
    rows = _open_items(platform, show)
    with db.cursor(commit=False) as cur:
        cur.execute("SELECT DISTINCT platform FROM open_items ORDER BY 1")
        platforms = [r["platform"] for r in cur.fetchall()]
    lag = config.SETTLEMENT_LAG_DAYS
    aw = f"Awaiting settlement (T+{lag})"
    buckets = {}
    for r in rows:
        if r["is_open"]:
            b = aw if r["awaiting_settlement"] else r["age_bucket"]
            x = buckets.setdefault(b, {"n": 0, "diff": 0.0})
            x["n"] += 1
            x["diff"] += float(abs(r["difference"] or 0))
    order = [aw, "0-30 days", "31-60 days", "61-90 days", "90+ days"]
    out = []
    for r in rows[:config.MAX_SCREEN_ROWS]:
        d = J(dict(r))
        d["status_label"] = export.label(r["status"])
        d["status_color"] = STATUS_COLORS.get(r["status"], "#888")
        out.append(d)
    return jsonify({"rows": out, "total": len(rows), "platforms": platforms, "lag_days": lag,
                    "buckets": [{"bucket": b, **buckets[b]} for b in order if b in buckets]})


@app.route("/api/data-check")
def data_check():
    with db.cursor(commit=False) as cur:
        profiles = profile_data.load(cur)
    period = request.args.get("period") or (next(iter(profiles)) if profiles else "")
    by_ds = profiles.get(period, {})
    days = []
    if period:
        first, last = profile_data.month_bounds(period)
        lo = min([first] + [r["min_date"] for r in by_ds.values() if r["min_date"]])
        hi = max([last] + [r["max_date"] for r in by_ds.values() if r["max_date"]])
        days = [(lo + timedelta(days=i)).isoformat() for i in range((hi - lo).days + 1)]
    bounds = profile_data.month_bounds(period) if period else None
    return jsonify({
        "periods": list(profiles.keys()), "period": period, "days": days,
        "month": [bounds[0].isoformat(), bounds[1].isoformat()] if bounds else None,
        "datasets": [{"name": d, "label": DATASETS[d]["label"], "profile": _profile_public(by_ds[d]) if d in by_ds else None}
                     for d in DATASET_ORDER],
        "cross_checks": profile_data.cross_checks(by_ds, period) if period else [],
    })


@app.route("/api/data-check/refresh", methods=["POST"])
def data_check_refresh():
    with db.cursor() as cur:
        profile_data.refresh_all(cur)
    return jsonify({"ok": True})


# --------------------------------------------------------------------------- Excel downloads
@app.route("/api/download/<int:run_id>")
def download(run_id):
    with db.cursor(commit=False) as cur:
        r = _run_row(cur, run_id)
    buf = export.build_workbook(r)
    return _send_xlsx(buf, f"ReconOne_SvS_run{run_id}_{carry.month_span(r, '%b%Y', '-')}.xlsx")


@app.route("/api/download/<int:run_id>/results")
def download_results(run_id):
    with db.cursor(commit=False) as cur:
        r = _run_row(cur, run_id)
    status = request.args.get("status") or ""
    exc = request.args.get("exc") or ""
    buf = export.build_results_workbook(r, status, (request.args.get("q") or "").strip(), exc)
    suffix = (f"_{status.strip('_').lower()}" if status else "") + (f"_{exc.strip('_').lower()}" if exc else "")
    return _send_xlsx(buf, f"ReconOne_SvS_results_run{run_id}_{carry.month_span(r, '%b%Y', '-')}{suffix}.xlsx")


@app.route("/api/open-items/download")
def open_items_download():
    platform = request.args.get("platform", "")
    show = request.args.get("show", "open")
    buf = export.build_open_items_workbook(_open_items(platform, show), config.SETTLEMENT_LAG_DAYS)
    return _send_xlsx(buf, f"ReconOne_open_items_{show}{'_' + platform if platform else ''}_{date.today():%Y%m%d}.xlsx")


if __name__ == "__main__":
    db.ensure_schema()
    log.info("ReconOne backend on http://127.0.0.1:%s  (database: %s)", os.getenv("RECONONE_PORT", "8000"),
             config.DATABASE_URL.rsplit("/", 1)[-1])
    app.run(debug=os.getenv("RECONONE_DEBUG") == "1", host="0.0.0.0", port=int(os.getenv("RECONONE_PORT", "8000")))
