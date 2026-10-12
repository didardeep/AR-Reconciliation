# AR Reconciliation Tool (Sales vs Settlement)

Flask + PostgreSQL implementation of the Myntra sales-vs-settlement reconciliation described in the BRD.

## Setup
1. Create the database in pgAdmin:  `CREATE DATABASE ar_recon;`
2. Set the connection string (or edit `config.py`):
   `set AR_RECON_DATABASE_URL=postgresql://postgres:<password>@localhost:5432/ar_recon`
3. `pip install -r requirements.txt`
4. `python app.py`  -> http://localhost:5000   (tables and helper functions are created automatically on start)

## Flow
1. **Upload data** - one Excel/CSV per dataset. Each lands in its own table with the exact file column names:
   `sales_forward`, `sales_rt`, `sales_rto`, `settlement_forward`, `settlement_reverse`.
   Headers not in `schema.py` are added to the table automatically (e.g. `Settlement_on_2024_02_02`);
   schema columns missing from the file are reported. Every row keeps `_row_no` (row in the file) so exceptions
   point at real rows.
2. **Run reconciliation** - pick the month, tolerance, return window, commission/tax tolerance and (optionally)
   the platform, then **map the columns**: two panels (Sales | Settlement) list every file with a dropdown per
   logical field (join key, amount, SKU, dates, ...) filled with that file's own column names. The SQL uses the
   columns picked here; nothing is hardcoded. Standard names are pre-selected, the last mapping is remembered,
   required fields block the run until chosen, optional fields left "not used" switch the related check off.
3. **Summary** - matched / unmatched / sales-only / settlement-only counts and amounts, exception counts by type.
4. **Results** - every row with status and net sales vs net settlement (paginated, filter by status, search by id).
5. **Exceptions** - grouped by type, each row shows source table, file row (click to see the full source row),
   ids, amounts and a plain-English description.
6. **Download Excel** (export.py) - sheets: Summary (total net sales, net settlement, difference, matched and
   mismatched amount, amounts by status), Hygiene Checks (every check per file with PASS / FAIL / NOT RUN),
   Results (all amounts per key + matched / mismatched amount + hygiene flags + TOTAL row), Exceptions,
   Hygiene Findings (row level, readable check names), Column Mapping (columns used in the run).
   Matched amount = net sales of Matched keys; Mismatched amount = |difference| of every other key.

## Engine (recon.py) - mirrors the tested pgAdmin SQL
* `net_sales`      : forward `shipment_value` - RT `shipment_value` (SUM by packet_id); RTO applied only when the
                     packet is not in RT.  Packets in both RT and RTO are flagged.
* `net_settlement` : forward `customer_paid_amount` - SUM(reverse `customer_paid_amount`) by order_release_id.
* comparison       : FULL OUTER JOIN on `packet_id = order_release_id`;
                     MATCHED if |net_sales - net_settlement| <= tolerance, else UNMATCHED; one-sided rows get
                     SALES_NOT_IN_SETTLEMENT / SETTLEMENT_NOT_IN_SALES.
The intermediate `net_sales` and `net_settlement` tables are left in the database after every run so they can
be checked in pgAdmin exactly like before.

## Checks (hygiene.py / recon.py)
Hygiene: EMPTY_DATASET, BLANK_COLUMN, BLANK_KEY_FIELD, BLANK_AMOUNT_FIELD, NON_NUMERIC_AMOUNT, UNPARSEABLE_DATE,
DATE_OUTSIDE_RECON_MONTH, DUPLICATE_KEY_SAME_STATUS, RETURN_AMOUNT_MISMATCH, RETURN_SKU_MISMATCH,
RETURN_OUTSIDE_WINDOW, MULTIPLE_RETURNS_PER_PACKET, RETURN_WITHOUT_FORWARD_SALE, PACKET_IN_BOTH_RT_AND_RTO.

Reconciliation: SHORT_SETTLEMENT, EXCESS_SETTLEMENT, SALES_NOT_IN_SETTLEMENT, SETTLEMENT_NOT_IN_SALES,
PACKET_ID_NOT_EQUAL_ORDER_RELEASE_ID, REVERSE_SETTLEMENT_WITHOUT_FORWARD, MULTIPLE_REVERSE_SETTLEMENTS,
RETURN_IN_SALES_BUT_NO_REVERSE_SETTLEMENT, REVERSE_SETTLEMENT_BUT_NO_RETURN_IN_SALES, COMMISSION_VARIANCE,
TAX_VARIANCE.

Which date column is used for the month check per dataset, and which columns are amount/date validated, is in
`config.py`.

## Column mapping (mapping.py)
`FIELDS` lists the logical fields per dataset and the standard column each one defaults to. A chosen column is
accepted only if it exists in that dataset's table and reaches SQL only as a quoted identifier
(`psycopg2.sql.Identifier`). The mapping of each run is stored in `recon_runs.column_mapping`; the last one in
`column_mappings`. Check results per run are in `recon_check_log`. New tables/columns are created on start.
