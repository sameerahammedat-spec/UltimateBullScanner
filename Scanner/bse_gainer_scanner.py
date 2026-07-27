"""
BSE Top Gainer Scanner — separate from market_scanner.py (NSE Nifty 500 scan).

WHY A SEPARATE SCANNER: many BSE Smallcap 250 constituents are BSE-only
listings (no NSE counterpart), so they never show up in the Nifty 500 scan at
all. This script scans a BSE universe directly, filters for stocks up
5%-20% today, and for the subset that are in BSE Smallcap 250 specifically,
builds a deep-dive table with trend/risk metrics AND a best-effort "why did
this gain" lookup from recent headlines.

Universe file format: same 3-column CSV as nifty500.csv / niftysmallcap250.csv
  Company Name, Industry, Symbol, ...
Save your BSE Smallcap 250 list as bse_smallcap250.csv (filename must contain
both "bse" and "smallcap" for the Cap Segment tagging in market_scanner.py to
label it correctly). See GETTING_BSE_SMALLCAP_LIST.md for how to get this file
if you don't have it yet (BSE blocks most scripted downloads, so this is
usually a short manual export).

Usage:
    python bse_gainer_scanner.py trading_toolkit.xlsx bse_smallcap250.csv [more_bse_universe.csv ...]

Gain window (edit if you want a different band):
    MIN_GAIN_PCT = 5.0
    MAX_GAIN_PCT = 20.0
"""

import sys
import time
import urllib.request
import urllib.parse
import xml.etree.ElementTree as ET
from datetime import date

from openpyxl import load_workbook
from openpyxl.styles import Font, PatternFill, Alignment, Border, Side
from openpyxl.utils import get_column_letter

from market_scanner import (
    FONT, BLACK, BLACK_B, HDR_FONT, HDR_FILL, SEC_FILL, SEC_FONT, ALT_FILL, BORDER,
    load_universe, analyze_symbol, analyze_universe_parallel, get_nifty_returns, score_row, verdict,
)

MIN_GAIN_PCT = 5.0
MAX_GAIN_PCT = 20.0
DEEPDIVE_CAP_SEGMENT = "BSE Smallcap"   # which Cap Segment gets the full deep-dive + news lookup

GAIN_HEADERS = ["Symbol", "Name", "Cap Segment", "Close", "% Chg", "Volume",
                "Turnover (Rs Cr)", "Vol Ratio (vs 3d avg)", "Vol Ratio (vs 20d avg)",
                "RSI(14)", "ADX(14)", "Score (0-9)", "Verdict"]

DEEPDIVE_HEADERS = ["Symbol", "Name", "Close", "% Chg", "Turnover (Rs Cr)",
                     "Vol Ratio (vs 3d avg)", "Vol Ratio (vs 20d avg)",
                     "Trend Strength", "20d Slope %/day", "Trend R^2",
                     "Trend Quality (ADX x R^2)", "Entry", "Stop Loss",
                     "Target 1", "Target 2", "Likely Reason for Gain", "Recent Headlines"]

# --- Best-effort "why did this stock gain" lookup ------------------------

REASON_KEYWORDS = {
    "Dividend / Bonus / Split": ["dividend", "bonus share", "stock split", "special dividend"],
    "Order / Contract Win": ["order win", "bags order", "contract win", "wins order",
                             "secures order", "receives order", "awarded contract",
                             "letter of intent", "lois worth"],
    "Results / Earnings": ["q1 results", "q2 results", "q3 results", "q4 results",
                           "net profit", "quarterly results", "profit jumps",
                           "profit rises", "profit surges", "beats estimates", "ebitda"],
    "Block / Bulk Deal": ["block deal", "bulk deal", "promoter buys", "stake buy"],
    "Rating Upgrade / Brokerage Call": ["upgrades", "rating upgrade", "target price raised",
                                        "buy rating", "brokerage", "initiates coverage"],
    "Stake Sale / M&A": ["acquire", "acquisition", "stake sale", "merger", "amalgamation",
                         "joint venture", " jv "],
    "Fundraise / QIP / Rights Issue": ["qip", "rights issue", "preferential issue",
                                       "raises funds", "fund raise"],
    "Regulatory / Govt / Approval": ["government order", "regulatory approval",
                                     "receives approval", "clearance", "patent granted"],
    "Buyback": ["buyback"],
}


def get_gain_reason(company_name, max_headlines=6, timeout=10):
    """Best-effort lookup of recent headlines via Google News RSS, then keyword
    matches them against common corporate-action categories. This is pattern
    matching on public headlines, NOT a guarantee of causation — always verify
    against the actual BSE/NSE corporate announcement before acting."""
    query = urllib.parse.quote(f'"{company_name}" share')
    url = f"https://news.google.com/rss/search?q={query}%20when%3A3d&hl=en-IN&gl=IN&ceid=IN:en"
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            data = resp.read()
        root = ET.fromstring(data)
        titles = [item.find("title").text for item in root.iter("item") if item.find("title") is not None]
        titles = titles[:max_headlines]
    except Exception:
        return "Could not fetch news (no internet or Google News blocked it)", []

    if not titles:
        return "No recent headlines found — verify BSE announcements manually; may be a pure technical/momentum breakout", []

    combined = " | ".join(titles).lower()
    matched = [reason for reason, kws in REASON_KEYWORDS.items() if any(kw in combined for kw in kws)]

    if not matched:
        return "No obvious corporate-action keyword in headlines — likely technical/momentum move; verify manually", titles

    return " / ".join(matched), titles


# --- Scan logic -----------------------------------------------------------

def run_gainer_scan(universe_paths, verbose=True):
    universe = load_universe(universe_paths)
    symbols = universe["symbol"].tolist()
    meta = universe.set_index("symbol").to_dict(orient="index")

    if verbose:
        print(f"BSE universe loaded: {len(symbols)} symbols")
    nifty_1m, nifty_3m = get_nifty_returns()

    gainers = []
    fetched = analyze_universe_parallel(symbols, nifty_1m, nifty_3m, verbose=verbose)
    for sym, d in fetched.items():
        if d is None or d["pct_change"] is None:
            continue
        if not (MIN_GAIN_PCT <= d["pct_change"] <= MAX_GAIN_PCT):
            continue

        d["symbol"] = sym
        d["name"] = meta.get(sym, {}).get("name", sym)
        d["sector"] = meta.get(sym, {}).get("sector", "")
        d["cap_segment"] = meta.get(sym, {}).get("cap_segment", "")
        d["score"] = score_row(d)
        d["verdict"] = verdict(d["score"])
        d["turnover_cr"] = (d["close"] * d["volume"]) / 1e7 if (d["close"] and d["volume"]) else None
        gainers.append(d)

    gainers = sorted(gainers, key=lambda x: x["pct_change"], reverse=True)

    deep_dive = []
    for d in gainers:
        if d.get("cap_segment") == DEEPDIVE_CAP_SEGMENT:
            reason, headlines = get_gain_reason(d["name"])
            d["gain_reason"] = reason
            d["headlines"] = "; ".join(h[:90] for h in headlines[:3]) if headlines else ""
            deep_dive.append(d)
            time.sleep(0.5)  # be polite to Google News

    return gainers, deep_dive


# --- Excel output ----------------------------------------------------------

def write_gainers_table(ws, start_row, rows, title=None):
    r = start_row
    if title:
        ws.cell(row=r, column=1, value=title).font = SEC_FONT
        for c in range(1, len(GAIN_HEADERS) + 1):
            ws.cell(row=r, column=c).fill = SEC_FILL
        ws.merge_cells(start_row=r, start_column=1, end_row=r, end_column=len(GAIN_HEADERS))
        r += 1
    for i, h in enumerate(GAIN_HEADERS, start=1):
        c = ws.cell(row=r, column=i, value=h)
        c.font = HDR_FONT
        c.fill = HDR_FILL
        c.alignment = Alignment(horizontal="center", vertical="center", wrap_text=True)
        c.border = BORDER
    ws.row_dimensions[r].height = 32
    r += 1

    def num(v, nd=2):
        return round(v, nd) if v is not None else "N/A"

    for idx, d in enumerate(rows):
        vals = [d["symbol"], d["name"], d.get("cap_segment", ""), num(d["close"], 2),
                num(d["pct_change"], 2), d["volume"], num(d.get("turnover_cr"), 2),
                num(d.get("vol_ratio_3d"), 2), num(d.get("vol_ratio_20d"), 2),
                num(d.get("rsi"), 1), num(d.get("adx"), 1), d["score"], d["verdict"]]
        for c, v in enumerate(vals, start=1):
            cell = ws.cell(row=r, column=c, value=v)
            cell.font = BLACK
            cell.border = BORDER
            cell.alignment = Alignment(horizontal="center")
            if idx % 2 == 1:
                cell.fill = ALT_FILL
        r += 1
    return r + 1


def write_gainer_deepdive_table(ws, start_row, rows, title=None):
    r = start_row
    if title:
        ws.cell(row=r, column=1, value=title).font = SEC_FONT
        for c in range(1, len(DEEPDIVE_HEADERS) + 1):
            ws.cell(row=r, column=c).fill = SEC_FILL
        ws.merge_cells(start_row=r, start_column=1, end_row=r, end_column=len(DEEPDIVE_HEADERS))
        r += 1
    for i, h in enumerate(DEEPDIVE_HEADERS, start=1):
        c = ws.cell(row=r, column=i, value=h)
        c.font = HDR_FONT
        c.fill = HDR_FILL
        c.alignment = Alignment(horizontal="center", vertical="center", wrap_text=True)
        c.border = BORDER
    ws.row_dimensions[r].height = 32
    r += 1

    def num(v, nd=2):
        return round(v, nd) if v is not None else "N/A"

    for idx, d in enumerate(rows):
        vals = [d["symbol"], d["name"], num(d["close"], 2), num(d["pct_change"], 2),
                num(d.get("turnover_cr"), 2), num(d.get("vol_ratio_3d"), 2), num(d.get("vol_ratio_20d"), 2),
                d.get("trend_strength", "N/A"), num(d.get("trend_slope_pct"), 2), num(d.get("trend_r2"), 2),
                num(d.get("trend_quality"), 1), num(d.get("entry"), 2), num(d.get("stop_loss"), 2),
                num(d.get("target1"), 2), num(d.get("target2"), 2),
                d.get("gain_reason", ""), d.get("headlines", "")]
        for c, v in enumerate(vals, start=1):
            cell = ws.cell(row=r, column=c, value=v)
            cell.font = BLACK
            cell.border = BORDER
            cell.alignment = Alignment(horizontal="center", wrap_text=True)
            if idx % 2 == 1:
                cell.fill = ALT_FILL
        r += 1
    return r + 1


def main(xlsx_path, universe_paths):
    gainers, deep_dive = run_gainer_scan(universe_paths)
    today = date.today()
    sheet_name = f"BSEGain_{today.strftime('%d%b%y')}"

    wb = load_workbook(xlsx_path)
    if sheet_name in wb.sheetnames:
        del wb[sheet_name]
    ws = wb.create_sheet(sheet_name)
    ws.sheet_view.showGridLines = False
    ws.cell(row=1, column=1, value=f"BSE Top Gainers ({MIN_GAIN_PCT}%-{MAX_GAIN_PCT}%) — {today.strftime('%d %b %Y')}").font = \
        Font(name=FONT, size=14, bold=True, color="1F3864")
    ws.merge_cells(start_row=1, start_column=1, end_row=1, end_column=len(GAIN_HEADERS))

    next_row = write_gainers_table(ws, 3, gainers,
        title=f"ALL BSE GAINERS {MIN_GAIN_PCT}%-{MAX_GAIN_PCT}% — {len(gainers)} stocks")
    write_gainer_deepdive_table(ws, next_row, deep_dive,
        title=f"BSE SMALLCAP GAINERS — DEEP DIVE + LIKELY REASON — {len(deep_dive)} stocks")

    for i in range(1, max(len(GAIN_HEADERS), len(DEEPDIVE_HEADERS)) + 1):
        ws.column_dimensions[get_column_letter(i)].width = 15
    ws.column_dimensions["B"].width = 26
    wb.save(xlsx_path)

    print(f"\nDone. Sheet '{sheet_name}' written: {len(gainers)} total gainers, "
          f"{len(deep_dive)} BSE Smallcap deep-dive picks with gain-reason lookup.")
    print("Reason tagging is keyword-matched from public headlines — treat it as a lead, not a fact; "
          "verify against the actual BSE corporate announcement before acting.")


if __name__ == "__main__":
    if len(sys.argv) < 3:
        print("Usage: python bse_gainer_scanner.py trading_toolkit.xlsx bse_smallcap250.csv [more.csv ...]")
        sys.exit(1)
    main(sys.argv[1], sys.argv[2:])
