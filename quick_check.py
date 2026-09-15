"""Quick check — run the reconciliation engine on Excel files with NO database
and NO web server, and print the computed results.

This is the fastest way to confirm the tool works on your data and that nothing
is hardcoded: point it at your files and read the numbers.

Usage:
    python quick_check.py \
        --sales-forward path/to/sales_forward.xlsx \
        --sales-rt       path/to/rt.xlsx \
        --sales-rto      path/to/rto.xlsx \
        --settle-forward path/to/settlement_forward.xlsx \
        --settle-reverse path/to/settlement_reverse.xlsx \
        --month 2024-02 --tolerance 1.0

Only --sales-forward and --settle-forward are required. Results and exceptions
are also written to CSV in the current folder.
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from app.services.run_service import run_reconciliation  # noqa: E402


def main() -> None:
    p = argparse.ArgumentParser(description="Run AR reconciliation on Excel files (no DB).")
    p.add_argument("--sales-forward", required=True)
    p.add_argument("--sales-rt")
    p.add_argument("--sales-rto")
    p.add_argument("--settle-forward", required=True)
    p.add_argument("--settle-reverse")
    p.add_argument("--link", help="optional packet_id<->order_release_id mapping file")
    p.add_argument("--month", default="2024-02")
    p.add_argument("--tolerance", type=float, default=1.0)
    a = p.parse_args()

    r = run_reconciliation(
        sales_forward_path=a.sales_forward,
        sales_rt_path=a.sales_rt,
        sales_rto_path=a.sales_rto,
        settlement_forward_path=a.settle_forward,
        settlement_reverse_path=a.settle_reverse,
        link_path=a.link,
        reconciliation_month=a.month,
        tolerance=a.tolerance,
    )

    print("\n=== SUMMARY CARDS ===")
    for k, v in r.summary_cards.items():
        print(f"  {k:22s} {v}")

    print("\n=== SALES (head) ===")
    print(r.sales.head(10).to_string(index=False))
    print("\n=== SETTLEMENT (head) ===")
    print(r.settlement.head(10).to_string(index=False))
    print("\n=== FINAL (head) ===")
    print(r.final.head(10).to_string(index=False))

    print("\n=== EXCEPTION SUMMARY ===")
    summ = r.exceptions.summary()
    print(summ.to_string(index=False) if not summ.empty else "  (none)")

    r.final.to_csv("final_results.csv", index=False)
    r.exceptions.to_frame().to_csv("exceptions.csv", index=False)
    print("\nWrote final_results.csv and exceptions.csv to the current folder.")


if __name__ == "__main__":
    main()
