"""Generate a Postgres VIEW per file_type flattening the data JSONB keys.

Usage:
    python scripts_make_view.py              # print SQL to stdout
    python scripts_make_view.py --execute    # create/replace views in the database

Each view exposes every JSONB key that appears in at least one row of the
given file_type as a real column, so browsing in pgAdmin looks like the
original spreadsheet.
"""
from __future__ import annotations

import argparse
import sys
from textwrap import dedent

from sqlalchemy import text

from app.db.session import engine
from app.db.models import RawRecord, UploadedFile


def _discover_keys(conn, file_type: str) -> list[str]:
    """Return sorted unique JSONB keys for a file_type."""
    sql = text(dedent("""\
        SELECT DISTINCT jsonb_object_keys(rr.data) AS k
        FROM raw_records rr
        JOIN uploaded_files uf ON uf.id = rr.file_id
        WHERE uf.file_type = :ft
        ORDER BY k
    """))
    rows = conn.execute(sql, {"ft": file_type}).fetchall()
    return [r[0] for r in rows]


def _view_sql(file_type: str, keys: list[str]) -> str:
    view_name = f"view_{file_type}"
    cols = ",\n    ".join(
        f"rr.data->>'{k}' AS \"{k}\"" for k in keys
    )
    return dedent(f"""\
        CREATE OR REPLACE VIEW {view_name} AS
        SELECT
            rr.id,
            rr.file_id,
            rr.row_index,
            {cols}
        FROM raw_records rr
        JOIN uploaded_files uf ON uf.id = rr.file_id
        WHERE uf.file_type = '{file_type}';
    """)


def main():
    parser = argparse.ArgumentParser(description="Generate JSONB-flattening VIEWs")
    parser.add_argument("--execute", action="store_true", help="Run the SQL against the database")
    args = parser.parse_args()

    with engine().connect() as conn:
        # Find all file_types that have rows
        ft_rows = conn.execute(text(
            "SELECT DISTINCT file_type FROM uploaded_files ORDER BY file_type"
        )).fetchall()
        file_types = [r[0] for r in ft_rows]

        if not file_types:
            print("No uploaded files found in the database.")
            return

        for ft in file_types:
            keys = _discover_keys(conn, ft)
            if not keys:
                print(f"-- {ft}: no data keys found, skipping", file=sys.stderr)
                continue
            sql = _view_sql(ft, keys)
            print(sql)
            if args.execute:
                conn.execute(text(sql))
                print(f"-- VIEW view_{ft} created ({len(keys)} columns)", file=sys.stderr)

        if args.execute:
            conn.commit()
            print("-- All views created successfully.", file=sys.stderr)


if __name__ == "__main__":
    main()
