# SKU & Quantity Check — Sales vs. Settlement

AR Reconciliation — Data Quality Review
Scope: Stage 1 (Sales) & Stage 3 (Final Match)
Status: Decisions pending BRD sign-off

## Why this came up

The BRD calls for a hygiene check confirming SKU returned matches SKU ordered, and that returned quantity never exceeds ordered quantity. That check was written into the codebase (`app/validation/sku_checks.py`) but never connected to the reconciliation run — it has no caller anywhere in the pipeline.

## What we've agreed

**1. Returned qty > ordered qty is a hard error, per SKU.**
Checked at `packet_id` + `sku_code` grain. A SKU can't be returned in a quantity greater than what was shipped for that packet — it's a physical impossibility, not a tolerance question.
Example: sell 4 units of a SKU on a packet, see 5 returned against it -> flagged, regardless of the dollar values involved.

**2. A SKU error forces the packet to EXCEPTION.**
Even when `net_sales` and `net_settlement` tie out within tolerance, a SKU qty violation overrides that and the packet's final status is set to `EXCEPTION`. A dollar match can hide a SKU-level data error (e.g. a return posted against the wrong line), so the dollar match alone isn't sufficient sign-off.

## Confirmed against the schema file (`data/ar_reco_schema.xlsx`)

Column counts previously quoted were from the dummy sample files, not the reference schema — corrected here against the actual schema workbook (sheets: `sale_forward`, `sale_RT`, `sale_rto`, `Settlement_forward`, `Settlement_reverse`):

| Sheet | Total columns | Quantity column present |
|---|---|---|
| sale_forward | 71 | `quantity` |
| sale_RT | 80 | `Qty` |
| sale_rto | 90 | `quantity` |
| Settlement_forward | 146 | **none** |
| Settlement_reverse | 147 | **none** |

This is no longer an open question — it's a confirmed constraint. Both settlement sheets carry `sku_code` and `return_type`, but neither has a quantity field of any kind. Settlement-side SKU checks can therefore only confirm a SKU is present/matches, not that the correct quantity was reversed, unless the client adds a qty field to the export (see question 1 below).

Note also: `return_type` already exists on `Settlement_forward`/`Settlement_reverse` — the same indicator column `rt_rto_selector.py` looks for on the sales side. Worth asking whether that field is reliably populated on settlement rows too.

Note on required columns: today the ingestion layer (`column_mapper.py::required_canonicals`) only hard-requires `packet_id`/`shipment_value` (sales) and `order_release_id`/`customer_paid_amt` (settlement) — `sku_code`/`qty` are resolved opportunistically but an upload isn't rejected if they're missing. Once this check is built, `sku_code`/`qty` should become required for the three sales files, or the check silently no-ops.

## Open items for the client to confirm

| # | Question | Why it matters |
|---|----------|-----------------|
| 1 | Settlement files have **no quantity column in the schema at all**. Can a quantity field be added to the settlement export, or is SKU presence-matching the ceiling on that side? | Without qty, settlement-side SKU checks can only confirm the SKU exists, not that the right amount was reversed. |
| 2 | What are the actual bucket definitions referenced in the BRD (aging / exception buckets)? | Buckets aren't specified anywhere we've found yet — needed to build the settlement-side classification. |
| 3 | Which date field defines "the month" for each file (order date, shipment date, restock date), and how should a transaction that straddles a month boundary be treated? | Drives the existing month-hygiene check; currently flags but doesn't drop out-of-month rows. |
| 4 | Confirmation that the Postgres license in use is free/open-source for this deployment. | Flagged as an open item in the original tech notes; not yet verified. |

## Data safety review (string/character handling)

Checked whether malformed or malicious characters in uploaded data could break reconciliation, the database, or the export path.

- **SQL injection — safe.** Column and table names are sanitized to `[a-z0-9_]` before being used as Postgres identifiers; cell values are always inserted through parameterized queries, never string-concatenated.
- **XSS in the frontend — safe.** No raw-HTML rendering anywhere in the React app; all displayed text is auto-escaped.
- **Excel/CSV formula injection — gap, not currently handled.** Cell values are stored and re-exported (`/export` endpoint) with no escaping. A field value starting with `=`, `+`, `-`, or `@` will be evaluated as a formula if the exported file is opened in Excel. Needs a fix before this ships to a client who will open exported files (prefix such values with `'` on export, or strip the leading character).
- **ID/join matching — mostly fine.** IDs are normalized (trailing `.0`, whitespace) before joins so stray formatting won't break a match. One minor spot: the free-text search filter on `/results` passes user input straight into a SQL `ILIKE`, so a literal `%` or `_` in a search term acts as a wildcard rather than a literal character — cosmetic, not a security issue.

## Implementation notes (internal, not for the client)

- **Sales side** — SKU code and quantity already survive ingestion into Postgres for Forward/RT/RTO. The check needs a `ReconciliationService` wrapping the existing `check_sku()` logic, wired into the run pipeline, writing to a new SKU-result table.
- **Settlement side** — `sku_code` is currently dropped during load (a one-line fix); quantity isn't available from source at all (confirmed above).
- **Export sanitization** — add formula-injection guarding to the `/export` endpoint before this is client-facing.
- A duplicate project tree exists at `ar-recon/ar-recon` — confirm which copy is canonical before this lands.
