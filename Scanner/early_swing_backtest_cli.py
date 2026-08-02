"""Command-line Phase-2 walk-forward backtest and calibration builder."""
from __future__ import annotations

import argparse
import logging
from dataclasses import replace
from pathlib import Path
from typing import List, Sequence

import pandas as pd

from early_swing_backtest import backtest_symbol, grouped_summary, summarize_trades, trades_to_frame
from early_swing_calibration import build_calibration, calibration_summary_frame, save_calibration
from early_swing_config import DEFAULT_EARLY_SWING_CONFIG
from early_swing_models import BacktestTrade
from early_swing_scanner import _fetch_benchmark, fetch_histories
from early_swing_universe import load_and_deduplicate_universes

LOGGER = logging.getLogger("early_swing_backtest")


def _chronological_holdout(trades: Sequence[BacktestTrade], fraction: float = 0.20) -> tuple[list[BacktestTrade], list[BacktestTrade]]:
    ordered = sorted(trades, key=lambda trade: (trade.signal_date, trade.symbol))
    if len(ordered) < 10:
        return ordered, []
    split = max(1, min(len(ordered) - 1, int(len(ordered) * (1 - fraction))))
    return ordered[:split], ordered[split:]


def run_backtest(args: argparse.Namespace) -> dict:
    config = replace(
        DEFAULT_EARLY_SWING_CONFIG,
        history_period=args.period,
        batch_size=max(1, args.batch_size),
        download_workers=max(1, args.workers),
        backtest_target_pct=args.target_pct,
        backtest_holding_sessions=args.calibration_holding,
        calibration_minimum_sample=args.minimum_sample,
        cache_hours=max(1, args.cache_hours),
    )
    records = load_and_deduplicate_universes(args.universes)
    if args.max_symbols > 0:
        records = records[:args.max_symbols]
        LOGGER.info("[test limit] Backtesting %d securities", len(records))
    else:
        LOGGER.info("Backtesting %d securities", len(records))

    benchmark = _fetch_benchmark(config)
    histories = fetch_histories(records, config, use_cache=not args.no_cache)

    if args.full_grid:
        entry_methods = config.backtest_entry_methods
        holding_windows = config.backtest_holding_windows
        target_methods = config.backtest_target_methods
    else:
        entry_methods = (args.entry_method,)
        holding_windows = (args.calibration_holding,)
        target_methods = (args.target_method,)

    all_trades: List[BacktestTrade] = []
    for idx, record in enumerate(records, 1):
        history = histories.get(record.stable_key)
        if history is None or history.empty:
            continue
        source_ticker = str(history.attrs.get("source_ticker") or record.preferred_ticker)
        record.ticker = source_ticker
        trades = backtest_symbol(
            record,
            history,
            benchmark,
            config,
            minimum_score=args.minimum_score,
            entry_methods=entry_methods,
            holding_windows=holding_windows,
            target_methods=target_methods,
        )
        all_trades.extend(trades)
        if idx % 25 == 0 or idx == len(records):
            LOGGER.info("Backtested %d/%d securities | %d trades", idx, len(records), len(all_trades))

    trades_frame = trades_to_frame(all_trades)
    trades_frame.to_csv(args.trades_output, index=False)

    summary = grouped_summary(
        all_trades,
        ("entry_method", "target_method", "holding_limit", "score_band", "setup_type", "market_regime"),
    )
    summary.to_csv(args.summary_output, index=False)

    calibration = build_calibration(
        all_trades,
        config,
        entry_method=args.entry_method,
        target_method=args.target_method,
        holding_limit=args.calibration_holding,
    )
    training, holdout = _chronological_holdout([
        trade for trade in all_trades
        if trade.entry_method == args.entry_method.upper()
        and trade.target_method == args.target_method.upper()
        and trade.holding_limit == args.calibration_holding
    ])
    calibration["chronological_validation"] = {
        "training": summarize_trades(training),
        "latest_20pct_holdout": summarize_trades(holdout),
        "holdout_trade_count": len(holdout),
        "note": "Rules are fixed; the latest chronological 20% is reported as an untouched diagnostic holdout.",
    }
    save_calibration(calibration, args.calibration_output)

    calibration_summary_frame(calibration).to_csv(args.calibration_buckets_output, index=False)
    overall = summarize_trades(all_trades)
    LOGGER.info(
        "Backtest complete: %d trades | win rate %.2f%% | expectancy %.3f%% | max DD %.2f%%",
        overall["trades"], overall["win_rate_pct"], overall["expectancy_pct"], overall["maximum_drawdown_pct"],
    )
    LOGGER.info("Calibration written to %s (%d canonical trades)", args.calibration_output, calibration["selected_trade_count"])
    return {"overall": overall, "calibration": calibration}


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Early Swing Phase-2 walk-forward backtest")
    parser.add_argument("universes", nargs="+", help="BSE 1000 / Group A / Group B CSV files")
    parser.add_argument("--period", default=DEFAULT_EARLY_SWING_CONFIG.calibration_history_period)
    parser.add_argument("--max-symbols", type=int, default=0)
    parser.add_argument("--batch-size", type=int, default=DEFAULT_EARLY_SWING_CONFIG.batch_size)
    parser.add_argument("--workers", type=int, default=DEFAULT_EARLY_SWING_CONFIG.download_workers)
    parser.add_argument("--minimum-score", type=float, default=DEFAULT_EARLY_SWING_CONFIG.watchlist_score)
    parser.add_argument("--entry-method", default="NEXT_OPEN", choices=["NEXT_OPEN", "SIGNAL_CLOSE", "ABOVE_SIGNAL_HIGH", "PULLBACK"])
    parser.add_argument("--target-method", default="FIXED_PERCENT", choices=["FIXED_PERCENT", "CANDIDATE_TARGET1", "CANDIDATE_TARGET2"])
    parser.add_argument("--calibration-holding", type=int, default=DEFAULT_EARLY_SWING_CONFIG.backtest_holding_sessions)
    parser.add_argument("--target-pct", type=float, default=DEFAULT_EARLY_SWING_CONFIG.backtest_target_pct)
    parser.add_argument("--minimum-sample", type=int, default=DEFAULT_EARLY_SWING_CONFIG.calibration_minimum_sample)
    parser.add_argument("--cache-hours", type=int, default=DEFAULT_EARLY_SWING_CONFIG.calibration_cache_hours)
    parser.add_argument("--full-grid", action="store_true", help="Evaluate all configured entry/target/holding combinations")
    parser.add_argument("--no-cache", action="store_true")
    parser.add_argument("--trades-output", default=DEFAULT_EARLY_SWING_CONFIG.calibration_trades_file)
    parser.add_argument("--summary-output", default=DEFAULT_EARLY_SWING_CONFIG.calibration_summary_file)
    parser.add_argument("--calibration-output", default=DEFAULT_EARLY_SWING_CONFIG.calibration_file)
    parser.add_argument("--calibration-buckets-output", default="early_swing_calibration_buckets.csv")
    parser.add_argument("--log-level", default="INFO", choices=["DEBUG", "INFO", "WARNING", "ERROR"])
    return parser


def main() -> None:
    args = build_parser().parse_args()
    logging.basicConfig(level=getattr(logging, args.log_level), format="%(asctime)s | %(levelname)s | %(message)s")
    run_backtest(args)


if __name__ == "__main__":
    main()
