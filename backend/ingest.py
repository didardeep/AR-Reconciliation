"""Upload handling: read the Excel/CSV, match headers to the schema, bulk-load into the dataset table."""
import json
import re
import logging

import pandas as pd

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger(__name__)

import config
import db
from schema import DATASETS, ID_COLUMNS

CHUNK = 20000


# BOM / zero-width / non-breaking characters that Excel and CSV exports put in front of the FIRST header
_INVISIBLE = re.compile("[\ufeff\u200b\u200c\u200d\u2060\u00a0]")


def _clean_header(h):
    return _INVISIBLE.sub("", str(h)).strip()


def _norm(h):
    """Normalise a header for matching: strip, collapse spaces to underscores, lower-case."""
    return re.sub(r"\s+", "_", _clean_header(h)).lower()


def _sanitize_new_column(h):
    h = re.sub(r"\s+", "_", _clean_header(h))
    h = re.sub(r"[^0-9A-Za-z_]", "_", h)
    # never start with "_": that prefix is reserved for _row_no / _source_file
    h = h.lstrip("_")
    return h[:63] or "unnamed_column"


def read_sheet_names(path):
    if path.lower().endswith((".csv", ".txt")):
        return []
    return pd.ExcelFile(path).sheet_names


def _read(path, sheet):
    if path.lower().endswith((".csv", ".txt")):
        return pd.read_csv(path, dtype=str, keep_default_na=False, encoding="utf-8-sig", encoding_errors="replace")
    return pd.read_excel(path, sheet_name=sheet or 0, dtype=str, keep_default_na=False)


def _clean_id(v):
    v = v.strip()
    if v.endswith(".0") and v[:-2].isdigit():
        return v[:-2]
    return v


# characters that are invisible on screen but make two values different (and break joins)
_HIDDEN = re.compile("[\u200b\u200c\u200d\u2060\ufeff\u00ad\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")


class Cleaner:
    """Cleans every value on the way in and counts what each fix changed (shown on the upload screen)."""

    LABELS = {
        "trimmed": "Spaces removed at start / end",
        "hidden": "Hidden characters removed (zero-width, BOM, control chars)",
        "apostrophe": "Leading apostrophe (') removed",
        "excel_decimal": "Trailing .0 removed from IDs",
        "uppercased": "IDs converted to upper case",
    }

    def __init__(self, upper_ids):
        self.upper_ids = upper_ids
        self.counts = dict.fromkeys(self.LABELS, 0)

    def value(self, v):
        if v is None:
            return ""
        v = str(v)
        if not v.isascii() or not v.isprintable():
            c = _HIDDEN.sub("", v)
            if c != v:
                self.counts["hidden"] += 1
                v = c
        s = v.strip()
        if s != v:
            self.counts["trimmed"] += 1
        if s[:1] == "'":
            self.counts["apostrophe"] += 1
            s = s[1:].strip()
        return s

    def ident(self, v):
        c = _clean_id(v)
        if c != v:
            self.counts["excel_decimal"] += 1
        if self.upper_ids:
            u = c.upper()
            if u != c:
                self.counts["uppercased"] += 1
            c = u
        return c

    def summary(self):
        return {self.LABELS[k]: n for k, n in self.counts.items() if n}


def _id_columns(cur, dataset):
    """Standard id columns + the columns mapped as key / packet ref / SKU on the Run screen."""
    cols = set(ID_COLUMNS)
    cur.execute("SELECT column_name FROM column_mappings WHERE dataset = %s AND role IN ('key','packet_ref','sku') "
                "AND column_name IS NOT NULL", (dataset,))
    cols |= {r["column_name"] for r in cur.fetchall()}
    return cols


def load_file(dataset, path, file_name, sheet=None, mode="replace", period=None):
    """Load one file into its dataset table.

    period: data month 'YYYY-MM'. Every month is kept; mode 'replace' deletes only the rows of that month.
    Returns a dict with rows_loaded, added_columns (headers in file but not in schema -> added to table),
    missing_columns (schema columns not present in the file -> left blank).
    """
    spec = DATASETS[dataset]
    log.info("Reading file '%s' (sheet=%s) for dataset '%s'...", file_name, sheet or "default", dataset)
    df = _read(path, sheet)
    df = df.dropna(how="all")
    total_rows = len(df)
    log.info("Read %d rows from '%s'", total_rows, file_name)
    # clean headers in place (BOM on the first column was making it a hidden "_Platform" column)
    df.columns = [_clean_header(c) for c in df.columns]
    file_headers = list(df.columns)

    with db.cursor() as cur:
        table_cols = db.existing_columns(cur, dataset)
        by_norm = {_norm(c): c for c in table_cols}

        mapping, added = {}, []          # file header -> table column
        for h in file_headers:
            if h.startswith("Unnamed:") and not (df[h].astype(str).str.strip() != "").any():
                continue        # blank header AND blank column -> skip; blank header with data is kept
            tc = by_norm.get(_norm(h))
            if tc is None:
                tc = _sanitize_new_column(h)
                if tc not in table_cols:
                    added.append(tc)
                    table_cols.append(tc)
                    by_norm[_norm(tc)] = tc
            mapping[h] = tc
        if added:
            db.add_columns(cur, dataset, added)

        present = set(mapping.values())
        missing = [c for c in spec["columns"] if c not in present]

        if mode == "replace":
            if period:
                db.delete_period(cur, dataset, period)     # other months stay
            else:
                db.truncate(cur, dataset)

        target_cols = ["_row_no", "_source_file", "_period"] + list(mapping.values())
        src_headers = list(mapping.keys())
        id_cols = _id_columns(cur, dataset)
        id_positions = [i for i, tc in enumerate(mapping.values()) if tc in id_cols]
        cleaner = Cleaner(config.UPPERCASE_IDS)

        n = 0
        total_chunks = (len(df) + CHUNK - 1) // CHUNK
        log.info("Starting bulk load: %d rows in %d chunk(s) of %d rows each", total_rows, total_chunks, CHUNK)
        for start in range(0, len(df), CHUNK):
            chunk_num = start // CHUNK + 1
            chunk = df.iloc[start:start + CHUNK][src_headers]
            rows = []
            for offset, vals in enumerate(chunk.itertuples(index=False, name=None)):
                vals = [cleaner.value(v) for v in vals]
                for i in id_positions:
                    vals[i] = cleaner.ident(vals[i])
                rows.append([start + offset + 2, file_name, period or ""] + vals)   # +2: header is row 1 in the file
            db.copy_rows(cur, dataset, target_cols, rows)
            n += len(rows)
            log.info("  Chunk %d/%d done — %d rows loaded so far", chunk_num, total_chunks, n)

        log.info("Finished loading '%s': %d total rows into '%s'", file_name, n, dataset)
        # table column -> header exactly as written in the file (feeds the column-mapping dropdowns)
        file_columns = json.dumps({tc: h for h, tc in mapping.items()})
        cur.execute("""INSERT INTO upload_log (dataset, file_name, sheet, rows_loaded, mode, added_columns, missing_columns,
                                               file_columns, period, cleaning)
                       VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)""",
                    (dataset, file_name, str(sheet or ""), n, mode, ", ".join(added), ", ".join(missing), file_columns,
                     period, json.dumps(cleaner.summary())))
        if period:
            import profile_data
            profile_data.refresh(cur, dataset, period)

    return {"rows_loaded": n, "added_columns": added, "missing_columns": missing, "cleaning": cleaner.summary()}
