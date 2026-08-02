"""Transparent no-look-ahead backtester for Early Swing setups."""
from __future__ import annotations

from dataclasses import asdict
from typing import Dict, List, Sequence

import pandas as pd

from early_swing_config import DEFAULT_EARLY_SWING_CONFIG, EarlySwingConfig
from early_swing_engine import analyze_early_swing
from early_swing_features import normalise_ohlcv
from early_swing_models import BacktestTrade, UniverseRecord


def backtest_symbol(record: UniverseRecord, stock_df: pd.DataFrame, benchmark_df: pd.DataFrame,
                    config: EarlySwingConfig = DEFAULT_EARLY_SWING_CONFIG,
                    minimum_score: float | None = None) -> List[BacktestTrade]:
    stock = normalise_ohlcv(stock_df)
    benchmark = normalise_ohlcv(benchmark_df)
    threshold = config.watchlist_score if minimum_score is None else minimum_score
    trades: List[BacktestTrade] = []
    minimum_index = max(config.minimum_history_sessions, 60)
    last_signal_index = len(stock) - config.backtest_holding_sessions - 2
    for signal_index in range(minimum_index, max(minimum_index, last_signal_index)):
        history = stock.iloc[:signal_index + 1]
        benchmark_history = benchmark.loc[:history.index[-1]] if len(benchmark) else benchmark
        candidate = analyze_early_swing(record, history, benchmark_history, config,
                                        analysis_date=history.index[-1].date())
        if candidate.rejected or candidate.rules_score < threshold or candidate.entry_low is None or candidate.stop_loss is None:
            continue
        entry_index = signal_index + 1
        if entry_index >= len(stock):
            continue
        entry_price = float(stock["Open"].iloc[entry_index])
        # Include slippage on entry and conservative round-trip costs at exit.
        entry_price *= 1 + config.backtest_slippage_pct / 100
        stop = float(candidate.stop_loss)
        target = entry_price * (1 + config.backtest_target_pct / 100)
        end = min(len(stock) - 1, entry_index + config.backtest_holding_sessions)
        result = "TIME_EXIT"
        exit_index = end
        exit_price = float(stock["Close"].iloc[end])
        for idx in range(entry_index, end + 1):
            low = float(stock["Low"].iloc[idx])
            high = float(stock["High"].iloc[idx])
            # Conservative ambiguity handling: when stop and target both trade on
            # the same daily candle, assume the stop occurred first.
            if low <= stop:
                result, exit_index, exit_price = "STOP", idx, stop
                break
            if high >= target:
                result, exit_index, exit_price = "TARGET", idx, target
                break
        exit_price *= 1 - (config.backtest_slippage_pct + config.backtest_cost_pct) / 100
        return_pct = (exit_price / entry_price - 1) * 100
        trades.append(BacktestTrade(
            symbol=record.symbol,
            signal_date=str(stock.index[signal_index].date()),
            entry_date=str(stock.index[entry_index].date()),
            entry_price=entry_price, stop_price=stop, target_price=target,
            exit_date=str(stock.index[exit_index].date()), exit_price=exit_price,
            result=result, return_pct=return_pct,
            holding_sessions=exit_index - entry_index + 1,
            score=candidate.rules_score, setup_type=candidate.setup_type,
        ))
    return trades


def summarize_trades(trades: Sequence[BacktestTrade]) -> Dict[str, float]:
    if not trades:
        return {"trades": 0, "win_rate_pct": 0.0, "average_return_pct": 0.0,
                "profit_factor": 0.0, "expectancy_pct": 0.0}
    returns = [t.return_pct for t in trades]
    gains = [r for r in returns if r > 0]
    losses = [r for r in returns if r <= 0]
    profit_factor = sum(gains) / abs(sum(losses)) if losses and sum(losses) != 0 else float("inf")
    return {
        "trades": len(trades),
        "win_rate_pct": len(gains) / len(trades) * 100,
        "average_return_pct": sum(returns) / len(returns),
        "average_gain_pct": sum(gains) / len(gains) if gains else 0.0,
        "average_loss_pct": sum(losses) / len(losses) if losses else 0.0,
        "profit_factor": profit_factor,
        "expectancy_pct": sum(returns) / len(returns),
    }
