# Wire ingestion through Postgres

## Context

Today, uploading a spreadsheet stores only **file metadata** in Postgres (`UploadedFile`: filename, stored_path, file_type). The rows never reach the database — the `.xlsx` sits on disk and is re-decoded by pandas on **every** reconciliation run (`run_service.py:64-69` calls `read_excel` six times). A Parquet cache partly masks this, but the database is not the system of record it appears to be, and the `SalesRecord`/`SettlementRecord` models clearly designed to hold rows are dead code nothing ever writes to.

The intended flow is **verify -> ingest -> process -> store**:

1. **Verify** — confirm required columns resolve (already exists: `check_file_readable`).
2. **Ingest** — load every row and **every column** into Postgres, once, at upload time.
3. **Process** — pull only the ~6 columns reconciliation needs into pandas; run the existing pipeline unchanged.
4. **Store** — results go to the existing result tables (unchanged).

Outcome: each spreadsheet is decoded exactly once, ever. Runs read a narrow projection from Postgres instead of re-parsing a workbook.

### Decisions already made
- **No JSONB for row data.** At hundreds of thousands of rows the original `.xlsx` on disk is already the audit trail; a JSONB copy costs 5-10x storage and buys nothing. Store real columns.
- **All columns ingested**, not just canonical ones. Narrowing happens on the way *out*, in the `SELECT`.
- **Upload stays synchronous**, but becomes non-blocking.
- **Re-upload does a full reload.** No incremental/delta sync.
- **Staging tables kept indefinitely**, with an explicit delete endpoint.
- **Result tables untouched.**

---

## Design

### Staging table per uploaded file

Each upload gets its own table in a dedicated **`staging` schema** (keeps `public` and `pg_dump` clean, and keeps `Base.metadata` honest since these tables are outside the ORM):

`staging."file_00000012"` — name derived from the surrogate PK, so it is unique, stable, always ≤63 chars, and never needs sanitizing.

| column | type | purpose |
|---|---|---|
| `_row_ord` | `INTEGER NOT NULL` | 0-based source row order; becomes the pandas index |
| `<slug>_<ord>` | `TEXT` | one per source header, in source order |

**No primary key, no indexes.** The only access pattern is a full `SELECT ... ORDER BY _row_ord`; a btree would slow `COPY` for no gain.

**Everything is `TEXT`, and this is load-bearing.** Three existing checks depend on seeing the original cell value:
- `data_quality.profile:109` separates `missing_amount` (NaN) from `invalid_amount` (non-numeric garbage) via `amt.isna() & df[amt_col].notna()`. A `NUMERIC` column collapses both to NULL and the distinction dies.
- `month_checks._parse_dates` raises `INVALID_DATE` on unparseable dates — a `DATE` column cannot hold the garbage that triggers it.
- IDs need `"12345.0"` vs `"12345"` normalization (`_norm_id`, `final.py:29`).

pandas does all `to_numeric`/date parsing after loading, exactly as today, so recon behavior is unchanged.

**`_row_ord` preserves source row order.** Exceptions reference rows as `f"row:{ref}"` from the DataFrame index (`sales.py:37`, `settlement.py:27`, `data_quality.py:91,113`, `month_checks.py:50-57`). The loader restores this as the index so those references keep pointing at the right spreadsheet row.

### Column naming: slug + ordinal suffix

`sanitize_header(raw, ordinal)`:
1. NFKD-normalize, ASCII-fold, lowercase, strip
2. non-`[a-z0-9_]` -> `_`, collapse runs, strip leading/trailing `_`
3. empty -> `col`; leading digit -> prefix `c_`
4. truncate to 58 chars
5. append `f"_{ordinal:04d}"`

Gives `order_release_id_0003`, `c_2024_sales_0017`, `col_0099`. **The ordinal guarantees uniqueness for free** — duplicate headers, over-long headers, empty headers and post-truncation collisions all resolve with no retry loop. Identifiers are *always* double-quoted in generated SQL, so reserved words (`order`, `select`) are safe.

Never f-string a raw header into SQL — headers come from an uploaded file, so that is an injection path. Quote and validate against the generated slug set.

### Header mapping on `UploadedFile`

New columns on `app/db/models.py::UploadedFile`:

```python
staging_schema:  Mapped[str | None]      = mapped_column(String(63))
staging_table:   Mapped[str | None]      = mapped_column(String(63))
sheet_name:      Mapped[str | None]      = mapped_column(String(255))
column_map:      Mapped[list | None]     = mapped_column(JSONB)
column_count:    Mapped[int | None]      = mapped_column(Integer)
ingest_error:    Mapped[str | None]      = mapped_column(Text)
ingested_at:     Mapped[datetime | None] = mapped_column(DateTime)
```

`column_map` is an ordered list, one entry per source column — **~130 entries of file metadata read once per run, not row data, so this is not the JSONB pattern that was rejected**:

```json
[{"ord": 0, "raw": "Order Release ID", "db": "order_release_id_0000", "canonical": "order_release_id"},
 {"ord": 2, "raw": "Warehouse",        "db": "warehouse_0002",        "canonical": null}]
```

`canonical` is resolved via `map_columns` at ingest time and frozen, so a later edit to `column_mappings.yaml` cannot retroactively change what an already-validated file means.

`row_count` and `processing_status` already exist and are already plumbed to the frontend — just populate them. Status lifecycle: `VALIDATED` -> `INGESTING` -> `INGESTED` | `INGEST_FAILED`.

---

## Implementation

### 1. `app/ingestion/column_mapper.py` — derive the column set from YAML

```python
@lru_cache
def all_canonicals(file_type: FileType) -> tuple[str, ...]:
    """Every canonical declared for this file type in column_mappings.yaml."""

def canonical_columns_for(file_type: FileType) -> list[str]:
    """Canonicals the recon pipeline may consume, incl. the configurable indicator."""
```

Do **not** hardcode a second list beside `required_canonicals()` — it will drift. The YAML already declares exactly the right set (required plus the optionals recon reads: `transaction_date`, `return_type`, `packet_id` on settlement files, `sku_code`/`qty`).

Must also append `settings.rt_rto_indicator_column`: `rt_rto_selector.decide` reads that **env-configurable** name off the reverse frames. If someone sets `RT_RTO_INDICATOR_COLUMN=reverse_flag`, a YAML-only list silently drops it and the selector falls back to presence-only — a wrong number with no error.

### 2. `app/ingestion/staging_store.py` (new) — pure mechanics, no FastAPI

```python
ROW_ORD = "_row_ord"

@dataclass(frozen=True)
class ColumnSpec:
    ord: int; raw: str; db: str; canonical: str | None

def sanitize_header(raw: str, ordinal: int) -> str
def build_column_specs(headers: list[str], file_type: FileType) -> list[ColumnSpec]
def staging_table_name(file_id: int) -> str          # "file_00000012"
def create_staging_table(cur, schema, table, specs) -> None   # DROP IF EXISTS + CREATE
def drop_staging_table(cur, schema, table) -> None
def copy_chunk(cur, schema, table, specs, df, row_offset) -> int
def insert_chunk_fallback(conn, schema, table, specs, df, row_offset) -> int   # sqlite tests
def load_frame(conn, rec: UploadedFile, columns=None) -> pd.DataFrame
```

**`copy_chunk`** builds an in-memory CSV via `df.to_csv(buf, index=False, header=False, na_rep="")` and hands it to psycopg2 `copy_expert` with `FORMAT csv, NULL ''`. `to_csv` is pandas' C writer — roughly an order of magnitude faster than a Python per-cell loop at 50k x 130, and it handles quoting/embedded newlines correctly. Plain `INSERT`s (what `pandas.to_sql` emits by default) are far too slow at this scale; **use `to_sql` for neither DDL nor data.**

Round-trip semantics to hold onto:
- `na_rep=""` + `NULL ''` maps NaN/NaT/None -> SQL NULL. Safe: an Excel blank already reads as NaN, so no genuine empty string can be confused with a blank.
- Integral floats serialize as `"12345.0"`; `_norm_id` already strips that tail for join keys. No behavior change.
- Non-numeric garbage (`"abc"`) round-trips as `"abc"`, so `profile()` still counts it `invalid_amount` not `missing_amount`. **This is the single most important invariant in the change.**

Guard: raise before DDL if `len(specs) > settings.ingest_max_columns` (default 1500), naming Postgres' 1600 hard cap.

**`load_frame`** selects only what recon needs, renaming in SQL:
```sql
SELECT "_row_ord", "order_release_id_0000" AS "order_release_id", ... 
FROM staging."file_00000012" ORDER BY "_row_ord"
```
then `df.set_index(ROW_ORD)` and `df.index.name = None` — reproducing `read_excel`'s bare RangeIndex exactly, so every `f"row:{ref}"` string stays byte-identical to today's. `map_columns` guarantees at most one raw header per canonical, so `AS` can never emit a duplicate column name.

### 3. `app/ingestion/excel_reader.py` — add `iter_raw_batches`

```python
def iter_raw_batches(path, sheet=None, chunk_rows=None) -> Iterator[pd.DataFrame]:
    """Yield chunks with ORIGINAL headers -- no canonical renaming."""
```

**Do not reuse `iter_row_batches`.** It calls `_apply_mapping()` and yields canonical-renamed frames; ingest stores every column and needs raw headers. Read once with `dtype=object` (keeps Excel's raw values, stops pandas coercing a mixed int/string ID column to float64), strip headers, slice by `chunk_rows`.

Everything else in this module stays untouched.

### 4. `app/services/ingest_service.py` (new) — orchestration and transactions

```python
def ingest_file(file_id, path, file_type, sheet=None) -> IngestReport
```

Opens its **own** connection via `engine().raw_connection()` — never the request session. `copy_expert` lives on the psycopg2 cursor, and doing DDL + COPY + commits through one DBAPI connection avoids fighting SQLAlchemy's transaction bookkeeping mid-loop.

- Phase A: `create_staging_table`, commit on its own
- Phase B: for each chunk from `iter_raw_batches`, `copy_chunk`, **commit per 50k chunk** (`settings.excel_chunk_rows`)
- Phase C: on any exception, rollback, drop the table best-effort on a fresh connection, re-raise

This is what constraint "one request = one transaction" demands: `get_session()` commits once after the route returns, so a large ingest inside it would pin one connection and transaction for the entire decode+insert, and a failure would roll back the metadata row too.

**`row_offset` is a running total across chunks, not each chunk's local index.** Getting this wrong silently corrupts every `row:{ref}` exception reference. Assert it in the round-trip test.

Chunked commits mean a mid-ingest crash can leave a partial table — which is why phase C drops it and status goes `INGEST_FAILED`. A partial table is never reachable because `_frame()` refuses any file not `INGESTED`.

### 5. `app/api/routes_files.py`

**`async def upload_file` -> `def upload_file`.** It already does blocking I/O (file copy, pandas decode, sync DB) on the event loop; adding a full ingest would stall the whole server. As a sync `def`, FastAPI runs it in a threadpool. `UploadFile.file` is the sync `SpooledTemporaryFile`, so the body needs no other change to drop `await`.

Ordered body:
1. Extension check (unchanged)
2. **Fix a real bug while here:** `routes_files.py:29` writes to `upload_dir / file.filename`, so a same-named upload silently **overwrites** the file on disk — and that file is now the audit trail for an already-ingested `file_id`. Use `f"{ts}_{uuid4().hex[:8]}_{safe_name}"`.
3. **Verify:** `check_file_readable(dest, file_type)` — unchanged, still headers-only, still the gate before expensive work. 422 on missing required columns.
4. Insert `UploadedFile(processing_status="INGESTING")`, `flush()`, then **explicitly `commit()`** so a durable `file_id` exists and metadata survives an ingest failure.
5. `ingest_service.ingest_file(...)` — outside the session.
6. Success: write `staging_schema`, `staging_table`, `column_map`, `row_count`, `column_count`, `sheet_name`, `ingested_at`, `processing_status="INGESTED"`.
7. Failure: set `INGEST_FAILED` + `ingest_error`, **`session.commit()` before raising** — `get_session()` rolls back on exception, so without this the failure status is lost and the row sticks at `INGESTING`.

Also: serve `file_columns` from `rec.column_map` instead of re-reading headers off disk, and add **`DELETE /api/files/{file_id}`** (drops the staging table + row). Retention is "keep indefinitely," so deletion must be deliberate and available.

### 6. `app/services/run_service.py` — paths to DataFrames

```python
def run_reconciliation(*, sales_forward: pd.DataFrame, settlement_forward: pd.DataFrame,
                       sales_rt=None, sales_rto=None, settlement_reverse=None, link=None,
                       reconciliation_month: str, tolerance: float | None = None) -> RunResult
```

Delete the six `read_excel` calls (lines 64-69) and the module-level import. Everything from line 71 down is untouched. Net effect: `run_service` depends on neither a database nor a filesystem — its docstring promise ("runs fully on pandas so it is unit-testable without a database") gets *stronger*.

Add an adapter so the DB-free path survives:
```python
def run_reconciliation_from_paths(*, sales_forward_path, settlement_forward_path, ...) -> RunResult
```
with a **lazy** `read_excel` import inside the function, keeping the core path free of `excel_reader`/`pyarrow` at module level.

`quick_check.py` changes two lines (import + call) to use it. It runs the whole engine with no DB and no server and must not regress.

### 7. `app/api/routes_reconciliation.py`

Replace `_path()` (line 23) with `_frame(session, file_id, expected: FileType)` calling `staging_store.load_frame(session.connection(), rec)`. `RunRequest` does **not** change shape — same six file IDs, only their resolution changes.

Two correctness wins beyond the storage change: validate that `rec.file_type` matches the slot (today you can pass a settlement file as `sales_rt_file_id` unchecked), and 409 on any file whose status isn't `INGESTED`.

### 8. Dead code

Delete `SalesRecord` and `SettlementRecord` (`app/db/models.py:52-71`) — never written, keyed by `run_id` (wrong grain; ingest happens at upload, before any run exists), and their JSONB `raw` column is precisely the design rejected above. Leaving them would mislead the next reader into thinking ingest writes there. Add `DROP TABLE IF EXISTS sales_records, settlement_records CASCADE` to the init script.

### 9. Schema rollout

`Base.metadata.create_all()` creates missing tables but **cannot ALTER** an existing `uploaded_files`. Extend `scripts_init_db.py` with idempotent DDL — correct on both a fresh and an existing database, all new columns nullable so no rewrite:

```python
Base.metadata.create_all(engine())
with engine().begin() as c:
    c.exec_driver_sql("CREATE SCHEMA IF NOT EXISTS staging")
    c.exec_driver_sql("ALTER TABLE uploaded_files ADD COLUMN IF NOT EXISTS column_map JSONB")
    ...  # one per new column, plus the DROP of the dead tables
```

Existing `UploadedFile` rows will 409 until re-uploaded. Given the dev database is disposable, say so in the README rather than building a backfill script.

**If Alembic is adopted later** (it's in `requirements.txt` with no `alembic.ini`), `migrations/env.py` **must** exclude the staging schema:
```python
def include_object(obj, name, type_, reflected, compare_to):
    return not (type_ == "table" and getattr(obj, "schema", None) == "staging")
```
Without this, autogenerate finds no model for any `staging.file_*` table and emits `op.drop_table` for all of them — the first migration would silently delete every ingested row.

### 10. Parquet cache: keep it, but don't use it for ingest

Clean division of labour — **Parquet serves the DB-free path, Postgres serves the API path.** `iter_raw_batches` reads the workbook directly and slices in memory, writing no cache: using the cache during ingest costs a full extra disk write plus read for zero benefit (`pd.read_excel` has no `chunksize`, so peak RAM is identical either way), and `_ensure_cache` does an extra `astype("string")` pass over ~130 columns. `read_excel`/`_ensure_cache`/`iter_row_batches` stay untouched for `quick_check.py`.

### 11. Frontend (`frontend/src/`) — small, mostly free

- `pages/FilesPage.jsx` — render `row_count` (already returned by the API, never displayed) and `processing_status` as a chip
- `pages/RunPage.jsx` — dropdowns currently list every file regardless of status; filter to `INGESTED`
- `api/client.js` — add `deleteFile(id)`

The 30-minute axios timeout is already in place and commented "for large files," so a synchronous ingest needs no other client change.

---

## Ordered sequence

1. `column_mapper`: `all_canonicals`, `canonical_columns_for`
2. `staging_store`: `sanitize_header`, `ColumnSpec`, `build_column_specs`, `staging_table_name` + `tests/test_staging_store.py` (no DB)
3. `excel_reader.iter_raw_batches`
4. `models.py`: new `UploadedFile` columns; delete the two dead models
5. `scripts_init_db.py`: schema + idempotent ALTERs + drops; run it
6. `staging_store`: `create_staging_table`, `copy_chunk`, `drop_staging_table`, `insert_chunk_fallback`
7. `ingest_service.ingest_file`
8. `staging_store.load_frame`
9. `run_service` signature + `run_reconciliation_from_paths`; **verify `quick_check.py` still works before moving on**
10. `routes_files`: sync, unique dest path, ingest, status transitions, `DELETE`
11. `routes_reconciliation`: `_frame()` replaces `_path()`
12. `tests/test_ingest_roundtrip.py`; frontend tweaks; README

Steps 1-3 and 9 touch no database and are independently verifiable — good early checkpoint.

---

## Verification

1. **`tests/test_staging_store.py` (no DB)** — `sanitize_header` against spaces, punctuation, unicode (`"Prix €"`), empty, `"2024 Sales"`, an 80-char header, `"select"`. `build_column_specs(["ID","ID","id",""])` yields four distinct `db` names. `canonical_columns_for(SALES_RT)` contains `return_type`, and contains `reverse_flag` when `RT_RTO_INDICATOR_COLUMN` is monkeypatched.
2. **`tests/test_run_service_frames.py` (no DB)** — first-ever coverage of `run_service`; call it with the existing `conftest.py` fixtures and assert the summary cards. ~10 lines, catches any signature slip.
3. **`tests/test_ingest_roundtrip.py`** — the important one. Build a tiny `.xlsx` containing a duplicate header, an 80-char header, `packet_id` values `12345` (numeric) and `"P002"` (text), `shipment_value` values `100` / `"abc"` / blank, `transaction_date` values `"2024-02-15"` / `"not a date"` / blank. Ingest -> `load_frame` -> assert: index is `range(n)` with `name is None`; `"abc"` survives as text; `profile()` reports `invalid_amount == 1` **and** `missing_amount == 1`; `check_month` produces one `INVALID_DATE`; columns are canonical and exclude unmapped junk; re-ingest is a clean full reload. Run against sqlite always (via `insert_chunk_fallback`) and against Postgres when `TEST_DATABASE_URL` is set.
4. **Existing suite** — `pytest tests/` stays green. All 22 recon tests use in-memory DataFrames and are insulated; the 3 `read_excel` tests still pass since that signature is unchanged.
5. **Behavioral regression check** — run `python quick_check.py --sales-forward <f> --settle-forward <f> --month YYYY-MM` **before and after** and diff `final_results.csv`. The numbers must be identical. This is the real proof that recon behavior is unchanged.
6. **End-to-end** — `docker compose up -d`, `python scripts_init_db.py`, `uvicorn app.main:app`, `npm run dev`. Upload each file type through the UI, confirm row counts and `INGESTED` status, verify in `psql` that `staging."file_*"` holds every source column, run a reconciliation, confirm the dashboard matches the `quick_check.py` numbers.
7. **Timing** — record wall-clock for upload (one-time decode) and for the run (should fall to seconds — a narrow ordered `SELECT`).

## Risks and honest limitations

- **All-TEXT means Postgres is a blob store with column names.** You get durability and no-re-decode-per-run, both real wins. You do *not* get useful SQL analytics — `SUM("customer_paid_amt_0007")` needs a cast and no index helps. If querying this in SQL is a goal, the follow-up is a *typed narrow projection* built after ingest, not changing the staging table's types (which would break the three checks above).
- **Ingesting ~130 columns to serve ~6 is ~20x more write work than recon needs.** Defensible as an audit trail, and paid once per file instead of once per run, so it still nets out positive — but ingest will not be fast.
- **Memory**: `pd.read_excel` materializes the whole frame; a 300 MB `.xlsb` can peak at several GB. True today as well, so not a regression. Real streaming would need row-level `pyxlsb`/`openpyxl` iteration feeding COPY directly — the follow-up if RAM becomes the wall.
- **Schema sprawl**: ~72 tables/year at 6 files/month, each ~130 columns, kept indefinitely by design. The `staging` schema and the `DELETE` endpoint contain it; revisit if upload volume grows.
- **Orphan tables**: if the request dies after the ingest commits, the table outlives its metadata row. `DELETE` covers manual cleanup; a sweep comparing `pg_tables` to `uploaded_files` would close it properly.
