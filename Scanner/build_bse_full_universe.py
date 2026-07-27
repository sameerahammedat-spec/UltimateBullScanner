"""
Builds a full-BSE scan universe (~5000+ stocks) directly from
bse_scrip_master.csv, instead of being limited to a single index list like
BSE Smallcap 250 (250 stocks). The master file already has real trading
symbols (Security Id), so no scrip-code resolution step is needed here -
this reads it directly.

USAGE:
    python build_bse_full_universe.py bse_scrip_master.csv

    Writes: bse_full_universe.csv (Company Name, Industry, Symbol, ISIN, Group)

By default this excludes BSE's Z, ZP, and T/TS groups - these are the
"restricted"/"trade-to-trade" segments BSE itself flags for governance
issues or extreme illiquidity, and they're disproportionately unlisted-on-
Yahoo or too thin to ever pass your MIN_VOLUME liquidity filter anyway, so
including them mostly just wastes scan time. Pass --all to include everything
if you want true zero-exclusion coverage.
"""

import sys
import pandas as pd

from fix_bse_universe import read_csv_safely, find_col

DEFAULT_EXCLUDED_GROUPS = {"Z", "ZP", "T", "TS"}


def build_full_universe(master_path, include_all=False):
    df = read_csv_safely(master_path)

    code_col = find_col(df, ("security code", "scrip code", "bse code"))
    sym_col = find_col(df, ("security id", "scrip id", "trading symbol"))
    name_col = find_col(df, ("issuer name", "security name", "company name", "name"))
    status_col = find_col(df, ("status",))
    instrument_col = find_col(df, ("instrument",))
    group_col = find_col(df, ("group",))
    isin_col = find_col(df, ("isin",))

    if not sym_col:
        raise ValueError(f"Could not find a Security Id / trading symbol column. "
                          f"Columns present: {list(df.columns)}")

    work = pd.DataFrame({
        "name": df[name_col].astype(str).str.strip() if name_col else "",
        "symbol": df[sym_col].astype(str).str.strip(),
        "status": df[status_col].astype(str).str.strip() if status_col else "Active",
        "instrument": df[instrument_col].astype(str).str.strip() if instrument_col else "Equity",
        "group": df[group_col].astype(str).str.strip() if group_col else "",
        "isin": df[isin_col].astype(str).str.strip() if isin_col else "",
    })

    before = len(work)
    work = work[work["status"].str.lower() == "active"]
    work = work[work["instrument"].str.lower() == "equity"]
    if not include_all:
        work = work[~work["group"].isin(DEFAULT_EXCLUDED_GROUPS)]
    work = work[work["symbol"].notna() & (work["symbol"] != "") & (work["symbol"] != "nan")]

    out = pd.DataFrame({
        "Company Name": work["name"],
        "Industry": work["group"],   # BSE's master list doesn't carry sector - Group is the closest available tag
        "Symbol": work["symbol"],
        "ISIN": work["isin"],
    })

    out_path = "bse_full_universe.csv"
    out.to_csv(out_path, index=False)
    print(f"{master_path}: {before} total securities -> {len(out)} in scan universe "
          f"({'all groups included' if include_all else f'excluded groups: {sorted(DEFAULT_EXCLUDED_GROUPS)}'})")
    print(f"-> Wrote {out_path}")
    return out_path


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("Usage: python build_bse_full_universe.py bse_scrip_master.csv [--all]")
        sys.exit(1)
    include_all = "--all" in sys.argv
    master_path = [a for a in sys.argv[1:] if not a.startswith("--")][0]
    build_full_universe(master_path, include_all=include_all)
