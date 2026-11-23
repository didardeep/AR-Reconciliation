# ReconOne — Sales vs Settlement (prototype backend + DB)

ReconOne UI with a live **Sales vs Settlement** module, backed by a Flask API on the **same `ar_recon` PostgreSQL database and tables** as the AR Recon tool — existing uploads, runs and open items show up as they are.
Other ReconOne modules (Revenue, Inventory, AR, …) are still the static demo.

```
reconone/
  backend/      Flask API (server.py) + reconciliation engine
  frontend/     ReconOne UI (index.html) — served by the backend at /
```

## Setup (one time)
1. Uses the existing **ar_recon** database (nothing to create; any missing table/column is added automatically on start, nothing is deleted).
2. `pip install -r requirements.txt`
3. If your Postgres password is not `postgres` (same variable as the AR Recon tool):
   - PowerShell: `$env:AR_RECON_DATABASE_URL="postgresql://postgres:YOUR_PASSWORD@localhost:5432/ar_recon"`

## Run
```
cd backend
python server.py
```
Open **http://127.0.0.1:8000** → sidebar **Sales vs Settlement**.

## Workflow (tabs)
1. **Upload data** – each file with its data month (Sales Forward / RT / RTO, Settlement Forward / Reverse).
2. **Run reconciliation** – one month or two consecutive months, tolerance, platform, column mapping.
3. **Dashboard** – KPIs, statuses, exceptions, checks, key-level results, drill-down to source rows, Excel.
4. **Open items** – carry-forward of unmatched keys, ageing, Excel.
5. **Data check** – dates / amounts per file per month, per-day coverage.
6. **Run history** – every run, open or download.

## Status logic
- Net sales = Sales Forward − |RT| (or − |RTO| if not in RT)
- Net settlement = Settlement Forward − |Reverse| (forward = 0 when only a reverse exists for a sold key)
- Order: Settlement not in sales → Sales not in settlement → Matched (|diff| ≤ tolerance) →
  Return processed but settlement not adjusted (net sales 0 with RT/RTO) → Sales higher → Settlement higher.
  Reverse-only keys with no sale: Reverse settlement without forward settlement.

## Database (pgAdmin)
Uploaded data: `sales_forward, sales_rt, sales_rto, settlement_forward, settlement_reverse` (column `_period` = data month).
Runs: `recon_runs, recon_results, recon_exceptions, recon_check_log, open_items, net_sales, net_settlement`.
Other: `upload_log, data_profile, column_mappings`.

## API
See the docstring at the top of `backend/server.py` (all endpoints under `/api`).
Config: `AR_RECON_DATABASE_URL`, `RECONONE_PORT` (default 8000), `RECONONE_MAX_UPLOAD_MB` (default 500).
