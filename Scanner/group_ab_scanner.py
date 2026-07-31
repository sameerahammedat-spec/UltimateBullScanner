"""
Group A/B Scanner - loads Group_A.csv + Group_B.csv, deduplicates into one
unique universe, runs the standard scan (reusing market_scanner.py's
existing fetch/indicator/scoring logic - no duplication), and includes the
Next-Day Explosive Move Watchlist.

Column detection is robust to whatever headers your Group A/B exports use
(same find_col() pattern as the rest of this codebase).

Usage:
    python group_ab_scanner.py trading_toolkit.xlsx Group_A.csv Group_B.csv
"""

import sys
from datetime import date

import pandas as pd
from openpyxl import load_workbook
from openpyxl.styles import Font
from openpyxl.utils import get_column_letter

from market_scanner import (
    FONT, load_universe, analyze_universe_parallel, get_nifty_returns,
    score_row, verdict, write_table, HEADERS as MARKET_HEADERS,
)
from explosive_config import DEFAULT_CONFIG
from explosive_engine import analyze_symbol_explosive, merge_duplicate_sources
from explosive_market_regime import classify_regime
from explosive_report import write_explosive_section
from explosive_calibration import run_symbol_calibration


def run_group_ab_scan(group_a_path, group_b_path, config=DEFAULT_CONFIG, run_explosive=True):
    universe_a = load_universe([group_a_path])
    universe_b = load_universe([group_b_path])
    print(f"Group A: {len(universe_a)} symbols | Group B: {len(universe_b)} symbols "
          f"(before dedup)")

    combined = pd.concat([universe_a.assign(_source="Group A"),
                           universe_b.assign(_source="Group B")], ignore_index=True)
    # merge_duplicate_sources needs {source: [symbols]}; build that, then
    # collapse combined back down to ONE row per unique symbol
    source_map = merge_duplicate_sources({
        "Group A": universe_a["symbol"].tolist(),
        "Group B": universe_b["symbol"].tolist(),
    })
    unique_universe = combined.drop_duplicates(subset="symbol", keep="first")
    print(f"Unique combined universe: {len(unique_universe)} symbols "
          f"({len(source_map) - len(unique_universe)} duplicates removed)")

    symbols = unique_universe["symbol"].tolist()
    meta = unique_universe.set_index("symbol").to_dict(orient="index")

    nifty_1m, nifty_3m = get_nifty_returns()
    fetched = analyze_universe_parallel(symbols, nifty_1m, nifty_3m, return_series=True, verbose=True)

    results = []
    for sym, d in fetched.items():
        if d is None or not d.get("liquidity_ok"):
            continue
        d["symbol"] = sym
        d["name"] = meta.get(sym, {}).get("name", sym)
        d["sector"] = meta.get(sym, {}).get("sector", "")
        d["score"] = score_row(d)
        d["verdict"] = verdict(d["score"])
        results.append(d)

    full_scan = sorted(results, key=lambda r: r["score"], reverse=True)

    explosive_candidates, rejected, backtest_pool = [], [], []
    if run_explosive:
        import yfinance as yf
        from market_scanner import _SHARED_SESSION
        nifty_hist = yf.Ticker("^NSEI", session=_SHARED_SESSION).history(period="1y", auto_adjust=True)
        above_50dma_flags = [d.get("above_50dma", False) for d in results]
        regime = classify_regime(nifty_hist["Close"], above_50dma_flags)

        for d in results[:config.top_n_report_limit * 3]:   # cap explosive analysis to top-scoring subset
            sym = d["symbol"]
            hist_df = yf.Ticker(sym.strip() + ".NS", session=_SHARED_SESSION).history(
                period=f"{config.calibration_years}y", auto_adjust=True)
            if hist_df.empty:
                continue
            cand = analyze_symbol_explosive(
                symbol=sym, name=d["name"], exchange="NSE/BSE",
                scanner_sources=source_map.get(sym, []), sector=d["sector"],
                df=hist_df, nifty_1m=nifty_1m, nifty_3m=nifty_3m, market_regime=regime,
                config=config, analysis_date=date.today(), run_calibration=True,
            )
            if cand.rejected:
                rejected.append(cand)
            else:
                explosive_candidates.append(cand)

        explosive_candidates.sort(key=lambda c: c.final_score, reverse=True)

    return full_scan, explosive_candidates, rejected


def main(xlsx_path, group_a_path, group_b_path):
    full_scan, explosive_candidates, rejected = run_group_ab_scan(group_a_path, group_b_path)

    today = date.today()
    sheet_name = f"GroupAB_{today.strftime('%d%b%y')}"
    wb = load_workbook(xlsx_path)
    if sheet_name in wb.sheetnames:
        del wb[sheet_name]
    ws = wb.create_sheet(sheet_name)
    ws.sheet_view.showGridLines = False
    ws.cell(row=1, column=1, value=f"Group A/B Combined Scan (deduplicated) — {today.strftime('%d %b %Y')}").font = \
        Font(name=FONT, size=14, bold=True, color="1F3864")
    ws.merge_cells(start_row=1, start_column=1, end_row=1, end_column=len(MARKET_HEADERS))

    next_row = write_table(ws, 3, full_scan,
        title=f"FULL SCAN (deduplicated Group A + B) — {len(full_scan)} liquid stocks")
    write_explosive_section(ws, next_row, explosive_candidates, rejected, [],
                             {"Qualifying explosive setups": len(explosive_candidates),
                              "Rejected candidates": len(rejected)},
                             today.strftime("%d %b %Y"))

    for i in range(1, len(MARKET_HEADERS) + 1):
        ws.column_dimensions[get_column_letter(i)].width = 14
    wb.save(xlsx_path)
    print(f"\nDone. Sheet '{sheet_name}': {len(full_scan)} scanned, "
          f"{len(explosive_candidates)} explosive candidates, {len(rejected)} rejected.")


if __name__ == "__main__":
    if len(sys.argv) != 4:
        print("Usage: python group_ab_scanner.py trading_toolkit.xlsx Group_A.csv Group_B.csv")
        sys.exit(1)
    main(sys.argv[1], sys.argv[2], sys.argv[3])
