"""
Recovery companion for bull_scanner.py.

If bull_scanner.py ever fails at the final "saving to Excel" step (usually
because the file was still open), your scan results are NOT lost — they're
backed up automatically to bull_scan_backup.pkl before the Excel write is
attempted. This script writes that backup into your Excel file, in seconds,
with zero network calls or re-fetching.

Usage:
    python write_backup_to_excel.py trading_toolkit.xlsx
"""

import sys
import pickle
from market_scanner import write_to_workbook


def main(xlsx_path):
    with open("bull_scan_backup.pkl", "rb") as f:
        data = pickle.load(f)

    print(f"Loaded backup from {data['date']} — "
          f"{len(data['full_scan'])} full-scan stocks, "
          f"{len(data['early_momentum'])} early-momentum candidates.")

    try:
        write_to_workbook(xlsx_path, data["sheet_name"], data["date"],
                           data["full_scan"], data["early_momentum"])
    except PermissionError:
        print(f"\n[!] Still couldn't save {xlsx_path} — the file is still open.")
        print("    Close it completely in Excel/LibreOffice, then run this script again.")
        sys.exit(1)

    print(f"Done. Sheet '{data['sheet_name']}' written to {xlsx_path}.")


if __name__ == "__main__":
    if len(sys.argv) != 2:
        print("Usage: python write_backup_to_excel.py trading_toolkit.xlsx")
        sys.exit(1)
    main(sys.argv[1])
