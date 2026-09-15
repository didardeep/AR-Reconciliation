# AR Reconciliation — Full Stack (Backend + Frontend)

Sales-vs-Settlement AR reconciliation for marketplace data. FastAPI + Pandas +
SQLAlchemy + PostgreSQL backend; React + Material UI (KPMG theme) frontend.
Bank reconciliation is not built yet but the architecture supports adding it as
one more modular stage.

```
ar-recon/
├── app/ config/ tests/ …     # backend (see full detail below)
├── frontend/                 # React + MUI dashboard
├── requirements.txt  scripts_init_db.py
└── README.md                 # this file
```

## How data flows (DB-backed)

```
Upload Excel  ->  parsed ONCE  ->  rows stored in PostgreSQL
                                   (sales_records / settlement_records:
                                    typed keys + full row as JSON `raw`)
Run           ->  rows SELECTed from PostgreSQL (fast, indexed)
              ->  reconciliation logic applied
              ->  results + exceptions written back, returned to the UI
```

The spreadsheet is decoded only at **upload** (the one slow step, done once).
Every reconciliation reads from the database — no re-reading Excel. You can
inspect the stored rows directly in pgAdmin.

> **Upgrading an existing database:** the `sales_records` / `settlement_records`
> tables changed (now keyed by `file_id`). Drop and recreate:
> `python scripts_init_db.py` on a fresh database, or drop those two tables in
> pgAdmin and re-run it.


## Quick start (two terminals)

**1) Backend**
```bash
cd ar-recon
python -m venv .venv && source .venv/bin/activate      # Windows: .venv\Scripts\activate
pip install -r requirements.txt
cp .env.example .env                                    # set DATABASE_URL to your local PostgreSQL
python scripts_init_db.py                               # create tables

# --- PostgreSQL without Docker ---
# 1. Install PostgreSQL from https://www.postgresql.org/download/ (ships with pgAdmin).
# 2. In pgAdmin (or psql) create a database, e.g. "ar_recon".
# 3. In .env set DATABASE_URL to that database, e.g.:
#      DATABASE_URL=postgresql+psycopg2://postgres:YOURPASSWORD@localhost:5432/ar_recon
# 4. Run: python scripts_init_db.py   (creates the tables; view them in pgAdmin)
uvicorn app.main:app --reload                           # http://localhost:8000/docs
```

**2) Frontend**
```bash
cd ar-recon/frontend
npm install
cp .env.example .env                                    # VITE_API_BASE_URL=http://localhost:8000
npm run dev                                             # http://localhost:5173
```

Then in the UI: **Files** (upload the 5 Excel files) → **Run Reconciliation**
(pick them, set month `2024-02`, tolerance `1.0`) → **Dashboard / Results /
Exceptions**.

## Core business logic

```
Net Sales      = Forward shipment_value − applicable RT OR RTO shipment_value   (by packet_id)
Net Settlement = Forward customer_paid_amt − Reverse customer_paid_amt          (by order_release_id)
Difference     = Net Sales − Net Settlement ;  MATCHED if abs(diff) ≤ tolerance else EXCEPTION
```

RT vs RTO is *decided* (indicator column, else packet-id presence), never both
subtracted; a packet in both is flagged AMBIGUOUS. `packet_id` and
`order_release_id` are not assumed equal — the final stage uses a configurable
mapping (link file or a settlement `packet_id` column); with neither, records
are flagged MISSING_MAPPING.

## Tests
```bash
cd ar-recon && pytest -q         # 26 tests, all green
```

---

## Backend detail

# AR Reconciliation — Sales vs Settlement (Phase 1)

Production-quality backend for reconciling marketplace **Sales** against
**Settlement**. Bank reconciliation is **not** implemented yet but the
architecture (modular `ReconciliationService` stages) is built so it can be
added as one more stage without restructuring.

## Core business logic (do not deviate)

```
Net Sales       = Forward shipment_value  −  applicable RT OR RTO shipment_value   (group by packet_id)
Net Settlement  = Forward customer_paid_amt  −  Reverse customer_paid_amt          (group by order_release_id)
Difference      = Net Sales − Net Settlement
Status          = MATCHED if abs(Difference) ≤ tolerance else EXCEPTION            (tolerance is configurable)
```

* **RT vs RTO is decided, never both subtracted.** Strategy is configurable
  (`RT_RTO_STRATEGY`): explicit indicator column first, else packet-id presence.
  A packet in both RT and RTO → `AMBIGUOUS`, value withheld, flagged.
* **`packet_id` ≠ `order_release_id`.** The final stage needs a mapping (a link
  file, or a `packet_id` column on the settlement file). With neither, every
  record is flagged `MISSING_MAPPING` — the app never invents the relationship.

## Stack

FastAPI · Pandas · SQLAlchemy 2.0 · Pydantic v2 · PostgreSQL. Config is
env-driven (`.env`); column mapping is data-driven (`config/column_mappings.yaml`).

## Layout

```
config/          settings (env) + column_mappings.yaml
app/core/        enums, logging
app/ingestion/   excel_reader (large-file streaming), column_mapper (configurable)
app/validation/  data_quality (Data Quality Summary), sku_checks
app/reconciliation/  base (abstract), rt_rto_selector, sales, settlement, month_checks, final
app/services/    run_service (orchestrator)
app/db/          base, session, models (all required tables, indexed)
app/api/         routes_files, routes_reconciliation
app/main.py      FastAPI app
tests/           unit tests for every spec scenario
```

## Setup

```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env                 # set DATABASE_URL to your local PostgreSQL
python scripts_init_db.py            # create tables (use Alembic for real migrations)
uvicorn app.main:app --reload        # http://localhost:8000/docs
```

## Reconciliation flow

`upload files → data-quality checks → month checks → sales recon → settlement
recon → final (sales vs settlement) → summary cards + exception summary →
export`. All stages record structured exceptions instead of dropping rows.

## API

```
POST /api/files/upload                 multipart: file, file_type
GET  /api/files
POST /api/reconciliation/run           body: RunRequest (month, tolerance, file ids)
GET  /api/reconciliation/{run_id}
GET  /api/reconciliation/{run_id}/sales
GET  /api/reconciliation/{run_id}/settlement
GET  /api/reconciliation/{run_id}/results       ?status=&exception_type=&q=
GET  /api/reconciliation/{run_id}/exceptions    ?code=
GET  /api/reconciliation/{run_id}/summary
GET  /api/reconciliation/{run_id}/export        ?kind=final|sales|settlement|exceptions&fmt=csv|excel
```

## Large-Excel performance

* `excel_reader.iter_row_batches()` streams a big sheet at constant memory
  (openpyxl read-only) in `EXCEL_CHUNK_ROWS` batches for bulk insert.
* Vectorised Pandas group-bys; no row-by-row Python loops in the hot path.
* PostgreSQL indexes on `packet_id`, `order_release_id`, transaction date and
  `run_id`; raw source rows preserved in JSONB for audit.

## Tests

```bash
pytest -q          # 26 tests: sales, settlement, final, ingestion — all scenarios
```

## Column mapping for this dataset

The sample Feb'24 files use `customer_paid_amount` as the sales value; it is
mapped to canonical `shipment_value` via `column_mappings.yaml` (`sales_*`
blocks). Adjust the aliases there if your production `shipment_value` is a
different column — no code change needed.
