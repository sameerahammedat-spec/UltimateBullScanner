"""
Fixes the #1 reason a BSE scan returns 0 stocks: BSE's own index-constituent
exports (like the one from bseindia.com's "Index" dropdown) list a numeric
SCRIP CODE in the "Symbol" column (e.g. 542772), not the trading symbol
Yahoo Finance needs (e.g. 360ONE). This script joins your index file against
BSE's official Scrip Code -> Trading Symbol master list and writes out a
clean, ready-to-scan CSV.

ONE-TIME SETUP - get the scrip master file:
    1. Go to https://www.bseindia.com/corporates/List_Scrips.aspx
    2. Segment: Equity | Status: Active | Group: leave blank (all groups)
    3. Click "Submit", then use the page's Export/Download to CSV/Excel option
    4. Save as bse_scrip_master.csv in this folder
    5. It must have (at least) these columns: Security Code, Security Id,
       Security Name, Status, ISIN No
    This only needs to be redone every few months (new listings/delistings).

USAGE:
    python fix_bse_universe.py bse_scrip_master.csv IndexConstituents_XXXX.csv

    Writes: IndexConstituents_XXXX_clean.csv with proper columns:
        Company Name, Industry, Symbol, ISIN

    Works for BSE Smallcap 250, BSE 1000, or any other BSE index export in
    the same "Constituents / Symbol(scrip code) / Sector" format.
"""

import sys
import warnings
import pandas as pd


def find_col(df, candidates):
    for c in df.columns:
        cl = c.strip().lower()
        for cand in candidates:
            # bidirectional substring check: catches both "Macro-Economic Sector"
            # (header contains the candidate) and "COMPANY" (candidate "company name"
            # contains the header) - a one-directional check misses the second case.
            if cand == cl or cand in cl or cl in cand:
                return c
    return None


def read_csv_safely(path):
    """BSE's own CSV exports sometimes have MORE comma-separated fields per data
    row than the header has column names (trailing blank columns from their
    export tool). pandas' default behavior in that case is to silently assume
    the *extra* fields are a row index - which shifts every single column's
    values without raising an error. index_col=False disables that guess so
    columns line up with their actual header names."""
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")  # the resulting "length mismatch" warning is expected/harmless here
        return pd.read_csv(path, index_col=False)


def load_scrip_master(path):
    df = read_csv_safely(path)
    code_col = find_col(df, ("security code", "scrip code", "bse code"))
    sym_col = find_col(df, ("security id", "scrip id", "trading symbol"))
    status_col = find_col(df, ("status",))
    isin_col = find_col(df, ("isin",))
    if not code_col or not sym_col:
        raise ValueError(
            f"Could not find scrip code / security id columns in {path}. "
            f"Found columns: {list(df.columns)}. Re-check the export from "
            f"bseindia.com/corporates/List_Scrips.aspx"
        )
    out = pd.DataFrame({
        "scrip_code": df[code_col].astype(str).str.strip(),
        "symbol": df[sym_col].astype(str).str.strip(),
        "status": df[status_col].astype(str).str.strip() if status_col else "Active",
        "isin": df[isin_col].astype(str).str.strip() if isin_col else "",
    })
    return out


def fix_universe_file(master, index_csv_path):
    df = read_csv_safely(index_csv_path)
    name_col = find_col(df, ("company name", "company", "constituents", "name", "security name"))
    code_col = find_col(df, ("symbol", "scrip code", "security code"))
    sector_col = find_col(df, ("sector", "industry"))

    if not code_col:
        raise ValueError(f"No scrip-code/Symbol column found in {index_csv_path}. "
                          f"Columns present: {list(df.columns)}")

    work = pd.DataFrame({
        "name": df[name_col].astype(str).str.strip() if name_col else "",
        "scrip_code": df[code_col].astype(str).str.strip(),
        "sector": df[sector_col].astype(str).str.strip() if sector_col else "",
    })

    merged = work.merge(master, on="scrip_code", how="left", suffixes=("", "_master"))

    unmatched = merged[merged["symbol"].isna()]
    matched = merged[merged["symbol"].notna()].copy()

    # Prefer Active listings if the master has duplicate scrip codes (rare, but happens
    # when a code was reused after a delisting)
    if "status" in matched.columns:
        matched = matched.sort_values("status", ascending=False)  # "Active" sorts after "Delisted" alphabetically is not guaranteed, so:
        matched["status_rank"] = (matched["status"].str.lower() == "active").astype(int)
        matched = matched.sort_values("status_rank", ascending=False).drop_duplicates("scrip_code")

    out = pd.DataFrame({
        "Company Name": matched["name"],
        "Industry": matched["sector"],
        "Symbol": matched["symbol"],
        "ISIN": matched.get("isin", ""),
    })

    out_path = index_csv_path.rsplit(".", 1)[0] + "_clean.csv"
    out.to_csv(out_path, index=False)

    print(f"{index_csv_path}: {len(df)} rows in, {len(out)} resolved to trading symbols, "
          f"{len(unmatched)} unmatched (delisted/renamed/scrip-code mismatch).")
    if len(unmatched) > 0:
        sample = unmatched["name"].head(10).tolist()
        print(f"  Sample unmatched names (first 10): {sample}")
    print(f"  -> Wrote {out_path}")
    return out_path


def main():
    if len(sys.argv) < 3:
        print("Usage: python fix_bse_universe.py bse_scrip_master.csv IndexConstituents_XXXX.csv [more.csv ...]")
        sys.exit(1)

    master_path = sys.argv[1]
    index_paths = sys.argv[2:]

    master = load_scrip_master(master_path)
    print(f"Loaded scrip master: {len(master)} securities from {master_path}\n")

    for p in index_paths:
        fix_universe_file(master, p)


if __name__ == "__main__":
    main()