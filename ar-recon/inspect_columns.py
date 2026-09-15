"""Inspect a spreadsheet's columns and show how they map — before running.

Handles .xlsx/.xlsm/.xlsb/.xls. Prints which headers resolve to canonical
names, which required ones are missing, and the closest candidate headers to
add as aliases in config/column_mappings.yaml.

Usage:
    python inspect_columns.py <file> <file_type>
    # file_type: sales_forward | sales_rt | sales_rto |
    #            settlement_forward | settlement_reverse | sales_settlement_link

Example:
    python inspect_columns.py "data/uploads/WB PG Forward Settlement.xlsb" settlement_forward
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from app.core.enums import FileType  # noqa: E402
from app.ingestion.column_mapper import resolution_report  # noqa: E402
from app.ingestion.excel_reader import read_headers  # noqa: E402


def main() -> None:
    if len(sys.argv) != 3:
        print(__doc__)
        sys.exit(1)
    path, ft = sys.argv[1], sys.argv[2]
    try:
        file_type = FileType(ft)
    except ValueError:
        print(f"Unknown file_type '{ft}'. One of: {[f.value for f in FileType]}")
        sys.exit(1)

    headers = read_headers(path)
    rep = resolution_report(headers, file_type)

    print(f"\nFile: {path}\nType: {ft}\nTotal columns: {len(headers)}")
    print("\n--- RESOLVED (source header -> canonical) ---")
    if rep["resolved"]:
        for raw, canon in rep["resolved"].items():
            print(f"  {raw!r:45s} -> {canon}")
    else:
        print("  (none resolved)")

    print("\n--- MISSING REQUIRED ---")
    if not rep["missing_required"]:
        print("  none — this file is ready to use.")
    else:
        for canon in rep["missing_required"]:
            print(f"  {canon}")
            for h, score in rep["suggestions"].get(canon, []):
                print(f"      candidate: {h!r}  (similarity {score})")
        print("\n  To fix: open config/column_mappings.yaml, find the")
        print(f"  '{file_type.value}:' block, and add the correct source header")
        print("  as an alias under the missing canonical name. Example:")
        print(f"    {rep['missing_required'][0]}: [ ..., \"Your Exact Header Name\" ]")

    print("\n--- ALL HEADERS ---")
    print("  " + " | ".join(str(h) for h in headers))


if __name__ == "__main__":
    main()
