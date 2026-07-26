"""
Resolves BSE-exported company names into the actual Yahoo Finance trading
symbols (e.g. "Aadhar Housing Finance Limited" -> "AADHARHFC"), by querying
Yahoo's own search endpoint. Prefers an NSE (.NS) match since that data tends
to be more reliable/liquid on Yahoo; falls back to BSE (.BO) if the company
is BSE-only.

WHY THIS EXISTS: an earlier version of this pipeline assumed the BSE Scrip
Code could be used directly as the .BO ticker (e.g. 544176.BO). That doesn't
hold reliably for many modern dual-listed smallcaps - this script replaces
that guess with an actual lookup instead.

I can't test this against live Yahoo servers from this environment, so
treat the first run as a trial run - it prints every company it COULDN'T
resolve at the end, so you can see exactly what needs a manual fix rather
than silently losing rows.

Setup (once):
    pip install requests pandas

Usage:
    python resolve_bse_symbols.py raw_bse_export.csv bse_smallcap250.csv
"""

import sys
import time
import requests
import pandas as pd

SEARCH_URL = "https://query1.finance.yahoo.com/v1/finance/search"
HEADERS = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"}


def find_col(df, candidates):
    for c in df.columns:
        if c.strip().lower() in candidates:
            return c
    return None


def clean_name(name):
    """Strip common corporate suffixes that hurt search matching."""
    name = name.strip()
    for suffix in [" LIMITED", " LTD.", " LTD", " Limited", " Ltd."]:
        if name.upper().endswith(suffix.upper()):
            name = name[: -len(suffix)]
    return name.strip()


def lookup_symbol(company_name, timeout=8):
    """Returns (base_symbol, exchange) or (None, None) if nothing usable found."""
    query = clean_name(company_name)
    try:
        resp = requests.get(SEARCH_URL, params={"q": query, "quotesCount": 6, "newsCount": 0},
                             headers=HEADERS, timeout=timeout)
        resp.raise_for_status()
        data = resp.json()
    except Exception as e:
        return None, f"request failed ({e})"

    quotes = data.get("quotes", [])
    if not quotes:
        return None, "no matches"

    # Prefer NSE (exchange code "NSI") over BSE ("BSE"), and equity type over others
    def rank(q):
        exch = q.get("exchange", "")
        qtype = q.get("quoteType", "")
        exch_score = 0 if exch == "NSI" else (1 if exch == "BSE" else 2)
        type_score = 0 if qtype == "EQUITY" else 1
        return (exch_score, type_score)

    candidates = sorted(quotes, key=rank)
    best = candidates[0]
    symbol = best.get("symbol", "")
    exch = best.get("exchange", "")
    if exch not in ("NSI", "BSE") or not symbol:
        return None, f"best match was exchange={exch}, symbol={symbol} — not NSE/BSE, skipped"

    base_symbol = symbol.split(".")[0]  # strip .NS/.BO — downstream scripts add it back
    return base_symbol, exch


def main(input_csv, output_csv):
    df = pd.read_csv(input_csv)
    company_col = find_col(df, ("company", "company name", "security name"))
    if not company_col:
        print(f"Could not find a company-name column. Found: {list(df.columns)}")
        sys.exit(1)

    resolved, failed = [], []
    total = len(df)
    for i, row in df.iterrows():
        name = str(row[company_col]).strip()
        print(f"[{i+1}/{total}] {name}...", end=" ", flush=True)
        symbol, info = lookup_symbol(name)
        if symbol:
            resolved.append({"Company Name": name, "Industry": "", "Symbol": symbol})
            print(f"-> {symbol} ({info})")
        else:
            failed.append({"Company Name": name, "reason": info})
            print(f"FAILED ({info})")
        time.sleep(0.3)  # be polite to Yahoo's search endpoint

    out = pd.DataFrame(resolved).drop_duplicates(subset="Symbol")
    out.to_csv(output_csv, index=False)
    print(f"\nResolved {len(out)}/{total} companies -> {output_csv}")

    if failed:
        fail_path = "bse_symbol_resolution_failures.csv"
        pd.DataFrame(failed).to_csv(fail_path, index=False)
        print(f"{len(failed)} companies could NOT be resolved automatically — see {fail_path}")
        print("You'll need to look those up manually (search the company name on")
        print("finance.yahoo.com directly, or on BSE India) and add them to")
        print(f"{output_csv} by hand: Company Name,Industry,Symbol")


if __name__ == "__main__":
    if len(sys.argv) != 3:
        print("Usage: python resolve_bse_symbols.py raw_bse_export.csv bse_smallcap250.csv")
        sys.exit(1)
    main(sys.argv[1], sys.argv[2])
