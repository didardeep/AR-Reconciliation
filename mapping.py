"""Column mapping: which column of each uploaded file plays which role in the reconciliation logic.

Nothing in recon.py / hygiene.py refers to a file column by a fixed name any more. Every query asks this
module for the column the user picked on the Run screen (a dropdown per logical field, per file) and the
name is dropped into the SQL as a quoted identifier.

Safety: a chosen name is only accepted when it is a real column of that dataset's table (checked against
information_schema) and it only ever reaches SQL through psycopg2.sql.Identifier - never by string
concatenation.
"""
import json
import re

from psycopg2 import sql

import config
import db
from schema import DATASET_ORDER, DATASETS, ID_COLUMNS

# The two columns of the mapping screen
SIDES = [
    ("Sales", ["sales_forward", "sales_rt", "sales_rto"]),
    ("Settlement", ["settlement_forward", "settlement_reverse"]),
]


def _f(role, label, default, required=False, help="", advanced=False):
    return {"role": role, "label": label, "default": default, "required": required, "help": help,
            "advanced": advanced}


def _sales_return_fields(dataset):
    ship_default, return_default = config.RETURN_WINDOW_DATES[dataset]
    return [
        _f("key", "Packet ID (join key)", "packet_id", True, "Matched to the Packet ID of Sales - Forward."),
        _f("amount", "Return amount", "shipment_value", True, "Deducted from forward sales to get net sales."),
        _f("sku", "SKU", "sku_id", help="Compared with the SKU on Sales - Forward."),
        _f("period_date", "Period date", config.MONTH_CHECK_COLUMNS[dataset],
           help="Must fall in the reconciliation month."),
        _f("ship_date", "Shipped date", ship_default, help="Start of the return window."),
        _f("return_date", "Return date", return_default, help="End of the return window."),
    ]


# Logical fields the engine needs from each file.  "default" is the column name that used to be hardcoded;
# it is only used to pre-select the dropdown.
FIELDS = {
    "sales_forward": [
        _f("key", "Packet ID (join key)", "packet_id", True,
           "Net sales is built per value of this column and joined to the settlement key."),
        _f("amount", "Sales amount", "shipment_value", True, "Forward sales value."),
        _f("sku", "SKU", "sku_id", help="Compared with the SKU on the return files."),
        _f("period_date", "Period date", config.MONTH_CHECK_COLUMNS["sales_forward"],
           help="Must fall in the reconciliation month."),
        _f("status", "Order status", "order_status", help="Duplicate check: same key and same status."),
    ],
    "sales_rt": _sales_return_fields("sales_rt"),
    "sales_rto": _sales_return_fields("sales_rto"),
    "settlement_forward": [
        _f("key", "Order release ID (join key)", "order_release_id", True,
           "Net settlement is built per value of this column and joined to the sales key."),
        _f("amount", "Settlement amount", "customer_paid_amount", True, "Forward settlement value."),
        _f("platform", "Platform", "Platform", help="Column the platform filter is applied to."),
        _f("packet_ref", "Packet ID in settlement file", "packet_id",
           help="Cross-checked against the order release ID."),
        _f("period_date", "Period date", config.MONTH_CHECK_COLUMNS["settlement_forward"],
           help="Must fall in the reconciliation month."),
        _f("status", "Order status", "order_status", help="Duplicate check: same key and same status."),
        _f("commission_amount", "Commission amount", "total_commission", advanced=True),
        _f("commission_pct", "Commission %", "commission_percentage", advanced=True),
        _f("taxable_amount", "Taxable amount", "taxable_amount", advanced=True),
        _f("tax_rate", "Total tax rate %", "total_tax_rate", advanced=True),
        _f("igst", "IGST amount", "igst_amount", advanced=True),
        _f("cgst", "CGST amount", "cgst_amount", advanced=True),
        _f("sgst", "SGST amount", "sgst_amount", advanced=True),
    ],
    "settlement_reverse": [
        _f("key", "Order release ID (join key)", "order_release_id", True,
           "Matched to the order release ID of Settlement - Forward."),
        _f("amount", "Reverse settlement amount", "customer_paid_amount", True,
           "Deducted from forward settlement to get net settlement."),
        _f("platform", "Platform", "Platform", help="Column the platform filter is applied to."),
        _f("packet_ref", "Packet ID in settlement file", "packet_id",
           help="Cross-checked against the order release ID."),
        _f("period_date", "Period date", config.MONTH_CHECK_COLUMNS["settlement_reverse"],
           help="Must fall in the reconciliation month."),
    ],
}

# Fields holding identifiers: get the same trailing ".0" clean-up the loader gives the standard id columns
ID_ROLES = ("key", "packet_ref", "sku")


def norm(name):
    """Compare column names ignoring case, spaces, underscores and punctuation."""
    return re.sub(r"[^0-9a-z]", "", str(name).lower())


def field(dataset, role):
    for f in FIELDS[dataset]:
        if f["role"] == role:
            return f
    raise KeyError(f"{dataset}.{role}")


class Mapping:
    """{dataset: {role: column name or None}} with helpers that hand out quoted identifiers."""

    def __init__(self, columns, headers=None):
        self.columns = {d: dict(columns.get(d, {})) for d in DATASET_ORDER}
        self.headers = headers or {}          # {dataset: {table column: header as written in the file}}

    def name(self, dataset, role):
        return self.columns.get(dataset, {}).get(role) or None

    def col(self, dataset, role):
        """Quoted identifier for SQL, or None when the field is not mapped."""
        n = self.name(dataset, role)
        return db.q(n) if n else None

    def header(self, dataset, role):
        n = self.name(dataset, role)
        return self.headers.get(dataset, {}).get(n, n) if n else None

    def to_json(self):
        return json.dumps({"columns": self.columns, "headers": self.headers})

    @classmethod
    def from_json(cls, text):
        if not text:
            return None
        d = json.loads(text)
        return cls(d.get("columns", {}), d.get("headers", {}))


def _table_columns(cur, dataset):
    return [c for c in db.existing_columns(cur, dataset) if c not in ("_row_no", "_source_file")]


def _row_count(cur, dataset):
    cur.execute(sql.SQL("SELECT count(*) AS n FROM {}").format(db.q(dataset)))
    return cur.fetchone()["n"]


def available_columns(cur, dataset):
    """[(table column, header as written in the file)] for the file(s) currently loaded in the dataset.

    Only columns that really came from the uploaded file are offered (the tables also carry the schema
    columns that the file did not have).  With nothing loaded, every table column is offered.
    """
    table_cols = _table_columns(cur, dataset)
    if _row_count(cur, dataset) == 0:
        return [(c, c) for c in table_cols]
    cur.execute("SELECT mode, file_columns, missing_columns FROM upload_log WHERE dataset = %s ORDER BY id DESC",
                (dataset,))
    headers = {}
    for r in cur.fetchall():
        if r["file_columns"]:
            for tc, h in json.loads(r["file_columns"]).items():
                headers.setdefault(tc, h)
        else:   # loaded before headers were recorded: every table column except those logged as missing
            missing = {c.strip() for c in (r["missing_columns"] or "").split(",")}
            for c in table_cols:
                if c not in missing:
                    headers.setdefault(c, c)
        if r["mode"] == "replace":      # earlier uploads were wiped by this one
            break
    if not headers:
        headers = {c: c for c in table_cols}
    known = set(table_cols)
    return [(c, h) for c, h in headers.items() if c in known]      # in the order of the file


def _match(default, columns):
    """Column among `columns` that is the default name (exact first, then ignoring case/spaces/underscores)."""
    if default in columns:
        return default
    want = norm(default)
    for c in columns:
        if norm(c) == want:
            return c
    return None


def load_saved(cur):
    cur.execute("SELECT dataset, role, column_name FROM column_mappings")
    out = {}
    for r in cur.fetchall():
        out.setdefault(r["dataset"], {})[r["role"]] = r["column_name"]
    return out


def save(cur, m):
    for dataset in DATASET_ORDER:
        for f in FIELDS[dataset]:
            cur.execute("""INSERT INTO column_mappings (dataset, role, column_name) VALUES (%s,%s,%s)
                           ON CONFLICT (dataset, role) DO UPDATE SET column_name = EXCLUDED.column_name,
                                                                     updated_at = now()""",
                        (dataset, f["role"], m.name(dataset, f["role"])))


def suggest(cur, submitted=None):
    """Everything the Run screen needs to draw the two mapping columns.

    Pre-selection order per field: what was just submitted (when re-drawing after an error) -> the mapping
    saved from the previous run, if that column is still in the file -> the standard column name, if the
    file has it -> nothing (the user must choose).
    """
    saved = load_saved(cur)
    sides = []
    for side, datasets in SIDES:
        block = []
        for d in datasets:
            cols = available_columns(cur, d)
            names = [c for c, _ in cols]
            fields = []
            for f in FIELDS[d]:
                if submitted is not None:
                    chosen = submitted.get(d, {}).get(f["role"]) or ""
                    chosen = chosen if chosen in names else ""
                elif f["role"] in saved.get(d, {}):
                    # a saved blank on an optional field means the user switched that check off on purpose
                    s = saved[d][f["role"]]
                    if s is None and not f["required"]:
                        chosen = ""
                    else:
                        chosen = s if s in names else (_match(f["default"], names) or "")
                else:
                    chosen = _match(f["default"], names) or ""
                fields.append({**f, "selected": chosen})
            block.append({"name": d, "label": DATASETS[d]["label"], "rows": _row_count(cur, d),
                          "columns": cols, "fields": fields})
        sides.append({"side": side, "datasets": block})
    return sides


def from_form(form):
    """Read the dropdowns of the Run screen: field names are map__<dataset>__<role>."""
    return {d: {f["role"]: (form.get(f"map__{d}__{f['role']}") or "").strip() for f in FIELDS[d]}
            for d in DATASET_ORDER}


def validate(cur, raw, platform=None):
    """Check a submitted mapping. Returns (Mapping, [error messages])."""
    errors, columns, headers = [], {}, {}
    for d in DATASET_ORDER:
        table_cols = set(_table_columns(cur, d))
        file_headers = dict(available_columns(cur, d))
        columns[d], headers[d] = {}, {}
        for f in FIELDS[d]:
            v = (raw.get(d, {}).get(f["role"]) or "").strip()
            if not v:
                columns[d][f["role"]] = None
                if f["required"]:
                    errors.append(f"{DATASETS[d]['label']}: choose a column for '{f['label']}'.")
                continue
            if v not in table_cols:     # never let an unknown name near the SQL
                errors.append(f"{DATASETS[d]['label']}: '{v}' is not a column of this file ({f['label']}).")
                columns[d][f["role"]] = None
                continue
            columns[d][f["role"]] = v
            headers[d][v] = file_headers.get(v, v)
    if platform:
        for d in ("settlement_forward", "settlement_reverse"):
            if not columns[d].get("platform"):
                errors.append(f"{DATASETS[d]['label']}: a platform filter is set, so choose the 'Platform' column.")
    return Mapping(columns, headers), errors


def default_mapping(cur):
    """Mapping made of the standard column names - what the engine used before columns became selectable."""
    columns = {}
    for d in DATASET_ORDER:
        table_cols = _table_columns(cur, d)
        columns[d] = {f["role"]: _match(f["default"], table_cols) for f in FIELDS[d]}
    return Mapping(columns)


def normalise_ids(cur, m):
    """Strip the trailing ".0" Excel puts on numeric ids, for mapped id columns the loader did not already
    clean (the loader only knows the standard id column names). Same rule as ingest._clean_id."""
    for d in DATASET_ORDER:
        done = set()
        for role in ID_ROLES:
            n = m.name(d, role)
            if not n or n in ID_COLUMNS or n in done:
                continue
            done.add(n)
            cur.execute(sql.SQL(r"""UPDATE {t} SET {c} = left(btrim({c}), -2)
                                    WHERE btrim({c}) ~ '^[0-9]+\.0$'""").format(t=db.q(d), c=db.q(n)))


def describe(m):
    """Rows for the 'Column Mapping' sheet / summary screen."""
    rows = []
    for side, datasets in SIDES:
        for d in datasets:
            for f in FIELDS[d]:
                rows.append({"side": side, "dataset": DATASETS[d]["label"], "field": f["label"],
                             "required": "Yes" if f["required"] else "No",
                             "file_column": m.header(d, f["role"]) or "(not mapped)",
                             "table_column": m.name(d, f["role"]) or ""})
    return rows
