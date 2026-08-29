"""Canonical Yahoo coverage diagnostic for UltimateBullScanner.

This diagnostic deliberately uses the SAME ticker candidate generator and history
validation as the production afternoon scanner. It reports daily and intraday
availability separately, so a valid company with no 5-minute bars is not labelled
as a bad ticker.
"""
from __future__ import annotations

import argparse
import logging
from pathlib import Path

import pandas as pd
import yfinance as yf

from afternoon_fresh_move_scanner import (
    DEFAULT_GROUP_A,
    DEFAULT_GROUP_B,
    DEFAULT_SMALLCAP,
    load_afternoon_universe,
)
from ticker_resolver import candidate_tickers, exchange_tickers, resolve_history, has_usable_ohlcv
from market_scanner import _SHARED_SESSION


def _fetch(yf_module, stock, period, interval):
    return resolve_history(
        yf_module, stock, period, interval,
        session=_SHARED_SESSION,
        retries=1,
    )


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", default="10d", help="Daily-history period, e.g. 10d")
    ap.add_argument("--intraday-period", default="1d", help="5m history period")
    ap.add_argument("--output", default="ticker_coverage_report_v4.csv")
    args = ap.parse_args()

    for name in ("yfinance", "yfinance.multi", "yfinance.scrapers"):
        logging.getLogger(name).setLevel(logging.CRITICAL)

    stocks = load_afternoon_universe(DEFAULT_GROUP_A, DEFAULT_GROUP_B, DEFAULT_SMALLCAP)
    print(f"Universe: {len(stocks)} company-equity rows")

    rows = []
    for idx, original in enumerate(stocks, 1):
        # Use independent clean copies for each interval. resolve_history mutates
        # resolved_ticker; sharing that field would make a daily result silently
        # bias the 5m candidate order and would invalidate the diagnostic.
        stock_daily = dict(original)
        stock_daily.pop("resolved_ticker", None)
        stock_daily.pop("resolution_status", None)
        stock_intraday = dict(original)
        stock_intraday.pop("resolved_ticker", None)
        stock_intraday.pop("resolution_status", None)
        candidates = candidate_tickers(stock_daily)
        exchanges = exchange_tickers(stock_daily)

        daily_ticker, daily_frame, daily_status = _fetch(yf, stock_daily, args.days, "1d")
        intraday_ticker, intraday_frame, intraday_status = _fetch(
            yf, stock_intraday, args.intraday_period, "5m"
        )

        daily_ok = has_usable_ohlcv(daily_frame)
        intraday_ok = has_usable_ohlcv(intraday_frame)

        if daily_ok and intraday_ok:
            status = "DAILY_AND_5M_OK"
        elif daily_ok and not intraday_ok:
            status = "DAILY_OK_5M_UNAVAILABLE"
        elif not daily_ok and not intraday_ok:
            status = "NO_USABLE_YAHOO_DATA"
        else:
            status = "5M_OK_DAILY_UNAVAILABLE"

        rows.append({
            "security_code": original.get("security_code", ""),
            "security_id": original.get("security_id", ""),
            "name": original.get("name", ""),
            "candidates": "|".join(candidates),
            "nse_candidate": exchanges["nse"],
            "bse_candidate": exchanges["bse"],
            "daily_resolved_ticker": daily_ticker,
            "daily_rows": len(daily_frame),
            "intraday_resolved_ticker": intraday_ticker,
            "intraday_rows": len(intraday_frame),
            "daily_status": daily_status,
            "intraday_status": intraday_status,
            "same_source": bool(daily_ticker and intraday_ticker and daily_ticker == intraday_ticker),
            "status": status,
        })

        if idx % 50 == 0:
            print(f"Checked {idx}/{len(stocks)}")

    df = pd.DataFrame(rows)
    df.to_csv(args.output, index=False)

    print("\nCoverage summary")
    print(df["status"].value_counts().to_string())
    print("\nResolved ticker summary")
    print(df["daily_resolved_ticker"].str[-3:].value_counts().to_string())
    print(f"\nSaved: {Path(args.output).resolve()}")


if __name__ == "__main__":
    main()
