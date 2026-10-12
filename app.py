"""AR Reconciliation tool - Flask front end.

Pages
  /                     upload the five datasets (Excel/CSV) into their PostgreSQL tables
  /run                  map the columns of each file (Sales | Settlement dropdowns), choose month / tolerance /
                        platform and run the reconciliation engine
  /summary/<run_id>     counts and amounts of matched / unmatched / one-sided, exceptions, every hygiene check
  /results/<run_id>     row-level result (status + net sales vs net settlement), paginated, filter by status
  /exceptions/<run_id>  every exception grouped by type, row-level detail, paginated
  /download/<run_id>    Excel workbook: Summary, Hygiene Checks, Results, Exceptions, Hygiene Findings, Column Mapping
"""
import time
import math
import os
import tempfile
from datetime import date, datetime

from flask import Flask, abort, flash, jsonify, redirect, render_template, request, send_file, url_for
from psycopg2 import sql

import config
import db
import export
import ingest
import mapping as colmap
import recon
from export import MATCHED_AMOUNT_SQL, MISMATCHED_AMOUNT_SQL, STATUS_LABELS
from schema import DATASET_ORDER, DATASETS

app = Flask(__name__)
app.secret_key = os.getenv("AR_RECON_SECRET", "ar-recon-dev-key")
app.config["MAX_CONTENT_LENGTH"] = 1024 * 1024 * 1024   # 1 GB uploads

@app.template_filter("money")
def money(v):
    if v is None:
        return ""
    return f"{float(v):,.2f}"


@app.template_filter("label")
def label(v):
    """Readable name of a status / hygiene check / exception code - the same names as the Excel output."""
    return export.label(v)


def _runs():
    with db.cursor(commit=False) as cur:
        cur.execute("SELECT * FROM recon_runs ORDER BY run_id DESC LIMIT 25")
        return cur.fetchall()


def _run(run_id):
    with db.cursor(commit=False) as cur:
        cur.execute("SELECT * FROM recon_runs WHERE run_id = %s", (run_id,))
        r = cur.fetchone()
    if not r:
        abort(404)
    return r


# --------------------------------------------------------------------------- upload
@app.route("/")
def index():
    counts, log = db.table_counts()
    return render_template("index.html", datasets=DATASETS, order=DATASET_ORDER, counts=counts, log=log)


@app.route("/upload", methods=["POST"])
def upload():
    dataset = request.form.get("dataset")
    mode = request.form.get("mode", "replace")
    sheet = request.form.get("sheet") or None
    f = request.files.get("file")
    if dataset not in DATASETS or not f or not f.filename:
        flash("Choose a dataset and a file.", "error")
        return redirect(url_for("index"))
    suffix = os.path.splitext(f.filename)[1].lower()
    if suffix not in (".xlsx", ".xlsm", ".xls", ".csv", ".txt"):
        flash("Only .xlsx / .xls / .csv files are accepted.", "error")
        return redirect(url_for("index"))
    with tempfile.NamedTemporaryFile(suffix=suffix, delete=False) as tmp:
        f.save(tmp.name)
        path = tmp.name
    try:
        if sheet and sheet.isdigit():
            sheet = int(sheet)
        res = ingest.load_file(dataset, path, f.filename, sheet=sheet, mode=mode)
        msg = f"{DATASETS[dataset]['label']}: loaded {res['rows_loaded']:,} rows from {f.filename} ({mode})."
        if res["added_columns"]:
            msg += f" Added {len(res['added_columns'])} new column(s) not in the schema: {', '.join(res['added_columns'][:12])}" + \
                   (" ..." if len(res["added_columns"]) > 12 else "")
        if res["missing_columns"]:
            msg += f" {len(res['missing_columns'])} schema column(s) were not in the file: {', '.join(res['missing_columns'][:12])}" + \
                   (" ..." if len(res["missing_columns"]) > 12 else "")
        flash(msg, "ok")
    except Exception as e:  # noqa: BLE001
        flash(f"Load failed: {e}", "error")
    finally:
        os.unlink(path)
    return redirect(url_for("index"))


@app.route("/clear/<dataset>", methods=["POST"])
def clear(dataset):
    if dataset in DATASETS:
        with db.cursor() as cur:
            db.truncate(cur, dataset)
        flash(f"{DATASETS[dataset]['label']} cleared.", "ok")
    return redirect(url_for("index"))


# --------------------------------------------------------------------------- run
def _platform_values(cur, dataset, column, limit=200):
    """Distinct values of a (validated) column - fills the platform filter."""
    if dataset not in DATASETS or column not in db.existing_columns(cur, dataset):
        return []
    cur.execute(sql.SQL("SELECT DISTINCT {c} AS p FROM {t} WHERE {c} IS NOT NULL AND btrim({c}) <> '' ORDER BY 1 LIMIT %s").format(
        c=db.q(column), t=db.q(dataset)), (limit,))
    return [r["p"] for r in cur.fetchall()]


def _run_page(submitted=None, values=None):
    """Draw the Run screen. submitted/values = what the user had picked when re-drawing after an error."""
    counts, _ = db.table_counts()
    with db.cursor(commit=False) as cur:
        sides = colmap.suggest(cur, submitted)
        platform_col = next((f["selected"] for side in sides for d in side["datasets"] if d["name"] == "settlement_forward"
                             for f in d["fields"] if f["role"] == "platform"), "")
        platforms = _platform_values(cur, "settlement_forward", platform_col) if platform_col else []
    values = values or {}
    if values.get("platform") and values["platform"] not in platforms:
        platforms.append(values["platform"])
    return render_template("run.html", counts=counts, datasets=DATASETS, order=DATASET_ORDER,
                           platforms=platforms, cfg=config, runs=_runs(), sides=sides, values=values,
                           default_month=values.get("month") or date.today().strftime("%Y-%m"))


@app.route("/run", methods=["GET", "POST"])
def run():
    if request.method == "POST":
        raw = colmap.from_form(request.form)
        try:
            month = datetime.strptime(request.form["month"], "%Y-%m").date().replace(day=1)
            tolerance = float(request.form.get("tolerance") or config.DEFAULT_TOLERANCE)
            window = int(request.form.get("return_window") or config.DEFAULT_RETURN_WINDOW_DAYS)
            comm_tol = float(request.form.get("commission_tol") or config.DEFAULT_COMMISSION_TOLERANCE)
            platform = (request.form.get("platform") or "").strip()
        except Exception as e:  # noqa: BLE001
            flash(f"Check the parameters: {e}", "error")
            return _run_page(raw, request.form)

        # The chosen columns must be real columns of the tables before they go anywhere near the SQL
        with db.cursor() as cur:
            m, errors = colmap.validate(cur, raw, platform)
            if not errors:
                colmap.save(cur, m)         # remembered as the default for the next run
        if errors:
            for e in errors:
                flash(e, "error")
            return _run_page(raw, request.form)

        try:
            run_id = recon.run_reconciliation(month, tolerance, window, comm_tol, platform, m)
            flash(f"Run #{run_id} completed.", "ok")
            return redirect(url_for("summary", run_id=run_id))
        except Exception as e:  # noqa: BLE001
            flash(f"Run failed: {e}", "error")
            return redirect(url_for("run"))

    return _run_page()


@app.route("/api/values/<dataset>")
def column_values(dataset):
    """Distinct values of one column of a dataset (used to refill the platform filter when the
    'Platform' column dropdown changes)."""
    with db.cursor(commit=False) as cur:
        return jsonify(_platform_values(cur, dataset, request.args.get("column") or ""))


# --------------------------------------------------------------------------- summary
@app.route("/summary")
def latest_summary():
    runs = _runs()
    if not runs:
        flash("No reconciliation has been run yet.", "error")
        return redirect(url_for("run"))
    return redirect(url_for("summary", run_id=runs[0]["run_id"]))


@app.route("/summary/<int:run_id>")
def summary(run_id):
    r = _run(run_id)
    with db.cursor(commit=False) as cur:
        cur.execute("""SELECT category, exception_type, count(*) AS n
                       FROM recon_exceptions WHERE run_id = %s GROUP BY 1,2 ORDER BY 1,3 DESC""", (run_id,))
        etypes = cur.fetchall()
        totals, grand = export.amount_totals(cur, run_id)
        checks = export.check_log(cur, run_id, "HYGIENE")
    m = colmap.Mapping.from_json(r.get("column_mapping"))
    return render_template("summary.html", run=r, etypes=etypes, totals=totals, grand=grand, checks=checks,
                           datasets=DATASETS, mapping_rows=colmap.describe(m) if m else [], runs=_runs())


# --------------------------------------------------------------------------- results
@app.route("/results/<int:run_id>")
def results(run_id):
    r = _run(run_id)
    status = request.args.get("status") or ""
    search = (request.args.get("q") or "").strip()
    page = max(int(request.args.get("page", 1)), 1)

    where, params = ["run_id = %s"], [run_id]
    if status:
        where.append("status = %s")
        params.append(status)
    if search:
        where.append("(packet_id ILIKE %s OR order_release_id ILIKE %s)")
        params += [f"%{search}%", f"%{search}%"]
    w = " AND ".join(where)
    with db.cursor(commit=False) as cur:
        cur.execute(f"SELECT count(*) AS n FROM recon_results WHERE {w}", params)
        total = cur.fetchone()["n"]
        shown = min(total, config.MAX_SCREEN_ROWS)
        pages = max(math.ceil(shown / config.PAGE_SIZE), 1)
        page = min(page, pages)
        cur.execute(f"""SELECT *, {MATCHED_AMOUNT_SQL} AS matched_amount, {MISMATCHED_AMOUNT_SQL} AS mismatched_amount
                        FROM recon_results WHERE {w}
                        ORDER BY CASE status WHEN 'UNMATCHED' THEN 0 WHEN 'SALES_NOT_IN_SETTLEMENT' THEN 1
                                             WHEN 'SETTLEMENT_NOT_IN_SALES' THEN 2 ELSE 3 END,
                                 abs(difference) DESC NULLS LAST, packet_id, order_release_id
                        LIMIT %s OFFSET %s""", params + [config.PAGE_SIZE, (page - 1) * config.PAGE_SIZE])
        rows = cur.fetchall()
    return render_template("results.html", run=r, rows=rows, total=total, shown=shown, page=page, pages=pages,
                           status=status, q=search, statuses=STATUS_LABELS, truncated=total > config.MAX_SCREEN_ROWS)


# --------------------------------------------------------------------------- exceptions
@app.route("/exceptions/<int:run_id>")
def exceptions(run_id):
    r = _run(run_id)
    etype = request.args.get("type") or ""
    category = request.args.get("category") or ""
    search = (request.args.get("q") or "").strip()
    page = max(int(request.args.get("page", 1)), 1)

    where, params = ["run_id = %s"], [run_id]
    if etype:
        where.append("exception_type = %s")
        params.append(etype)
    if category:
        where.append("category = %s")
        params.append(category)
    if search:
        where.append("(packet_id ILIKE %s OR order_release_id ILIKE %s OR description ILIKE %s)")
        params += [f"%{search}%"] * 3
    w = " AND ".join(where)
    with db.cursor(commit=False) as cur:
        cur.execute("""SELECT category, exception_type, count(*) AS n FROM recon_exceptions
                       WHERE run_id = %s GROUP BY 1,2 ORDER BY 1, 3 DESC""", (run_id,))
        groups = cur.fetchall()
        cur.execute(f"SELECT count(*) AS n FROM recon_exceptions WHERE {w}", params)
        total = cur.fetchone()["n"]
        shown = min(total, config.MAX_SCREEN_ROWS)
        pages = max(math.ceil(shown / config.PAGE_SIZE), 1)
        page = min(page, pages)
        cur.execute(f"""SELECT * FROM recon_exceptions WHERE {w}
                        ORDER BY category, exception_type, dataset, row_no, packet_id, order_release_id
                        LIMIT %s OFFSET %s""", params + [config.PAGE_SIZE, (page - 1) * config.PAGE_SIZE])
        rows = cur.fetchall()
    return render_template("exceptions.html", run=r, rows=rows, groups=groups, total=total, shown=shown, page=page,
                           pages=pages, etype=etype, category=category, q=search, datasets=DATASETS,
                           truncated=total > config.MAX_SCREEN_ROWS)


# --------------------------------------------------------------------------- source row drill-down
@app.route("/row/<dataset>/<int:row_no>")
def source_row(dataset, row_no):
    if dataset not in DATASETS:
        abort(404)
    with db.cursor(commit=False) as cur:
        cur.execute(sql.SQL("SELECT * FROM {} WHERE {} = %s").format(db.q(dataset), db.q("_row_no")), (row_no,))
        row = cur.fetchone()
    if not row:
        abort(404)
    return render_template("row.html", dataset=dataset, label=DATASETS[dataset]["label"], row=row)


# --------------------------------------------------------------------------- download
EXPORT_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "exports")


def _send_xlsx(buf, name):
    """Send the workbook to the browser AND keep a copy in ./exports (in case the browser does not save it)."""
    os.makedirs(EXPORT_DIR, exist_ok=True)
    path = os.path.join(EXPORT_DIR, name)
    with open(path, "wb") as f:
        f.write(buf.getbuffer())
    app.logger.warning("Excel saved: %s", path)
    buf.seek(0)
    return send_file(buf, as_attachment=True, download_name=name,
                     mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")


@app.route("/download/<int:run_id>")
def download(run_id):
    r = _run(run_id)
    t = time.time()
    app.logger.warning("Excel for run #%s: building...", run_id)
    buf = export.build_workbook(r)
    app.logger.warning("Excel for run #%s: ready in %.1fs (%.1f MB)", run_id, time.time() - t,
                       len(buf.getbuffer()) / 1e6)
    return _send_xlsx(buf, f"AR_reconciliation_run{run_id}_{r['recon_month']:%b%Y}.xlsx")


@app.route("/download/<int:run_id>/results")
def download_results(run_id):
    """Results rows only (with the status / search filter of the Results screen)."""
    r = _run(run_id)
    status = request.args.get("status") or ""
    buf = export.build_results_workbook(r, status, (request.args.get("q") or "").strip())
    suffix = f"_{status.lower()}" if status else ""
    return _send_xlsx(buf, f"AR_results_run{run_id}_{r['recon_month']:%b%Y}{suffix}.xlsx")


if __name__ == "__main__":
    db.ensure_schema()
    app.run(debug=True, host="0.0.0.0", port=5000)
