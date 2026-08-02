"""Transparent, chronological backtesting for Early Swing setups.

Phase 2 expands the original single-method helper into a walk-forward engine that
supports multiple entry methods, holding windows, and target methods. Every
signal is calculated from data ending on the signal date; future candles are used
only for entry/exit simulation.
"""
from __future__ import annotations

from dataclasses import asdict
from typing import Dict, Iterable, List, Optional, Sequence, Tuple

import numpy as np
import pandas as pd

from early_swing_config import DEFAULT_EARLY_SWING_CONFIG, EarlySwingConfig
from early_swing_engine import analyze_early_swing
from early_swing_features import normalise_ohlcv
from early_swing_models import BacktestTrade, UniverseRecord


def score_band(score: float, config: EarlySwingConfig = DEFAULT_EARLY_SWING_CONFIG) -> str:
    for lower, upper, label in config.calibration_score_bands:
        if lower <= score <= upper:
            return label
    return "UNKNOWN"


def liquidity_bucket(stock: pd.DataFrame, signal_index: int) -> str:
    start = max(0, signal_index - 19)
    window = stock.iloc[start:signal_index + 1]
    traded_value_cr = float((window["Close"] * window["Volume"]).median() / 1e7)
    if traded_value_cr >= 100:
        return "VERY_HIGH"
    if traded_value_cr >= 20:
        return "HIGH"
    if traded_value_cr >= 5:
        return "MEDIUM"
    return "LOW"


def _candidate_signal_indices(stock: pd.DataFrame, config: EarlySwingConfig) -> List[int]:
    """Vectorised, no-look-ahead prefilter for expensive full-rule evaluation.

    The full analyzer is intentionally strict, but calling it on every historical
    day is unnecessarily expensive. This prefilter keeps only days that could
    plausibly be a breakout (or the following day for rally-age-two analysis).
    Every rolling level is shifted by one session, so the current/future candle
    cannot leak into the prior resistance or volume baseline.
    """
    close = stock["Close"]
    high = stock["High"]
    low = stock["Low"]
    volume = stock["Volume"]
    previous_close = close.shift(1)
    return_pct = (close / previous_close - 1) * 100
    prior_volume = volume.shift(1).rolling(20, min_periods=10).median()
    volume_ratio = volume / prior_volume.replace(0, np.nan)
    candle_range = (high - low).replace(0, np.nan)
    clv = (close - low) / candle_range

    breakout_possible = pd.Series(False, index=stock.index)
    for lookback in config.breakout_lookbacks:
        prior_pivot = high.shift(1).rolling(lookback, min_periods=lookback).max()
        breakout_possible |= (
            (close >= prior_pivot * (1 + config.breakout_buffer_pct / 100))
            | ((high >= prior_pivot * (1 + config.breakout_buffer_pct / 100)) & (close >= prior_pivot))
        )

    mask = (
        breakout_possible
        & (return_pct >= config.breakout_min_return_pct)
        & (volume_ratio >= config.breakout_min_volume_ratio)
        & (clv >= config.minimum_close_location_value)
    ).fillna(False)
    indices = set(int(i) for i in np.flatnonzero(mask.to_numpy()))
    # The next completed session may be a valid rally-age-two follow-through.
    indices.update(i + 1 for i in list(indices) if i + 1 < len(stock))
    return sorted(indices)


def _candidate_entry(
    candidate,
    stock: pd.DataFrame,
    signal_index: int,
    entry_method: str,
    config: EarlySwingConfig,
) -> Optional[Tuple[int, float]]:
    """Return (entry_index, gross entry price) without using future information.

    The candidate was created only from data through ``signal_index``. The entry
    simulation then inspects future candles according to the selected method.
    """
    method = entry_method.upper()
    if method == "SIGNAL_CLOSE":
        price = float(stock["Close"].iloc[signal_index])
        return signal_index, price

    next_index = signal_index + 1
    if next_index >= len(stock):
        return None

    if method == "NEXT_OPEN":
        return next_index, float(stock["Open"].iloc[next_index])

    if method == "ABOVE_SIGNAL_HIGH":
        trigger = max(
            float(stock["High"].iloc[signal_index]) * (1 + config.entry_buffer_pct / 100),
            float(candidate.entry_low or 0.0),
        )
        # Allow up to three sessions for the trigger, but never chase above the
        # candidate's pre-computed maximum entry boundary.
        last_index = min(len(stock) - 1, signal_index + 3)
        for idx in range(next_index, last_index + 1):
            if float(stock["High"].iloc[idx]) < trigger:
                continue
            fill = max(float(stock["Open"].iloc[idx]), trigger)
            if candidate.entry_high is not None and fill > float(candidate.entry_high):
                return None
            return idx, fill
        return None

    if method == "PULLBACK":
        lower = float(candidate.entry_low or 0.0)
        upper = float(candidate.entry_high or lower)
        last_index = min(len(stock) - 1, signal_index + 3)
        for idx in range(next_index, last_index + 1):
            low = float(stock["Low"].iloc[idx])
            high = float(stock["High"].iloc[idx])
            if high >= lower and low <= upper:
                fill = min(max(float(stock["Open"].iloc[idx]), lower), upper)
                return idx, fill
        return None

    raise ValueError(f"Unsupported entry method: {entry_method}")


def _target_price(candidate, entry_price: float, target_method: str,
                  config: EarlySwingConfig) -> float:
    method = target_method.upper()
    if method == "FIXED_PERCENT":
        return entry_price * (1 + config.backtest_target_pct / 100)
    if method == "CANDIDATE_TARGET1":
        target = float(candidate.target1 or 0.0)
        return target if target > entry_price else entry_price * (1 + config.backtest_target_pct / 100)
    if method == "CANDIDATE_TARGET2":
        target = float(candidate.target2 or 0.0)
        return target if target > entry_price else entry_price * (1 + config.backtest_target_pct / 100)
    raise ValueError(f"Unsupported target method: {target_method}")


def _simulate_exit(
    stock: pd.DataFrame,
    entry_index: int,
    entry_price: float,
    stop: float,
    target: float,
    holding_sessions: int,
) -> Tuple[str, int, float, float, float]:
    end = min(len(stock) - 1, entry_index + holding_sessions - 1)
    result = "TIME_EXIT"
    exit_index = end
    exit_price = float(stock["Close"].iloc[end])
    max_high = entry_price
    min_low = entry_price

    for idx in range(entry_index, end + 1):
        low = float(stock["Low"].iloc[idx])
        high = float(stock["High"].iloc[idx])
        max_high = max(max_high, high)
        min_low = min(min_low, low)
        # Conservative daily-bar ambiguity rule: if target and stop are both
        # touched on the same candle, assume the stop happened first.
        if low <= stop:
            result, exit_index, exit_price = "STOP", idx, stop
            break
        if high >= target:
            result, exit_index, exit_price = "TARGET", idx, target
            break

    mfe = (max_high / entry_price - 1) * 100
    mae = (min_low / entry_price - 1) * 100
    return result, exit_index, exit_price, mfe, mae


def backtest_symbol(
    record: UniverseRecord,
    stock_df: pd.DataFrame,
    benchmark_df: pd.DataFrame,
    config: EarlySwingConfig = DEFAULT_EARLY_SWING_CONFIG,
    minimum_score: float | None = None,
    entry_methods: Optional[Sequence[str]] = None,
    holding_windows: Optional[Sequence[int]] = None,
    target_methods: Optional[Sequence[str]] = None,
) -> List[BacktestTrade]:
    """Chronologically evaluate one symbol with expanding historical windows."""
    stock = normalise_ohlcv(stock_df)
    benchmark = normalise_ohlcv(benchmark_df)
    threshold = config.watchlist_score if minimum_score is None else minimum_score
    entries = tuple(entry_methods or config.backtest_entry_methods)
    windows = tuple(holding_windows or config.backtest_holding_windows)
    targets = tuple(target_methods or config.backtest_target_methods)
    trades: List[BacktestTrade] = []

    if stock.empty or len(stock) < config.minimum_history_sessions + max(windows, default=1) + 3:
        return trades

    minimum_index = max(config.minimum_history_sessions, 60)
    last_signal_index = len(stock) - max(windows, default=1) - 3
    last_accepted_signal = -10_000

    potential_indices = [
        index for index in _candidate_signal_indices(stock, config)
        if minimum_index <= index <= last_signal_index
    ]

    for signal_index in potential_indices:
        history = stock.iloc[:signal_index + 1]
        benchmark_history = benchmark.loc[:history.index[-1]] if len(benchmark) else benchmark
        candidate = analyze_early_swing(
            record,
            history,
            benchmark_history,
            config,
            analysis_date=history.index[-1].date(),
        )
        if candidate.rejected or candidate.rules_score < threshold:
            continue
        if candidate.entry_low is None or candidate.stop_loss is None:
            continue
        if signal_index - last_accepted_signal < config.backtest_minimum_gap_sessions:
            continue
        last_accepted_signal = signal_index

        for entry_method in entries:
            entry = _candidate_entry(candidate, stock, signal_index, entry_method, config)
            if entry is None:
                continue
            entry_index, raw_entry_price = entry
            entry_price = raw_entry_price * (1 + config.backtest_slippage_pct / 100)
            stop = float(candidate.stop_loss)
            if stop >= entry_price:
                continue

            for target_method in targets:
                target = _target_price(candidate, entry_price, target_method, config)
                if target <= entry_price:
                    continue
                for holding_limit in windows:
                    result, exit_index, raw_exit_price, mfe, mae = _simulate_exit(
                        stock,
                        entry_index,
                        entry_price,
                        stop,
                        target,
                        int(holding_limit),
                    )
                    gross_return = (raw_exit_price / entry_price - 1) * 100
                    total_cost = config.backtest_slippage_pct + config.backtest_cost_pct
                    exit_price = raw_exit_price * (1 - total_cost / 100)
                    net_return = (exit_price / entry_price - 1) * 100
                    trades.append(BacktestTrade(
                        symbol=record.symbol,
                        signal_date=str(stock.index[signal_index].date()),
                        entry_date=str(stock.index[entry_index].date()),
                        entry_price=entry_price,
                        stop_price=stop,
                        target_price=target,
                        exit_date=str(stock.index[exit_index].date()),
                        exit_price=exit_price,
                        result=result,
                        return_pct=net_return,
                        holding_sessions=exit_index - entry_index + 1,
                        score=candidate.rules_score,
                        setup_type=candidate.setup_type,
                        rally_age=int(candidate.rally_age or 0),
                        market_regime=candidate.market_regime,
                        score_band=score_band(candidate.rules_score, config),
                        entry_method=entry_method.upper(),
                        target_method=target_method.upper(),
                        holding_limit=int(holding_limit),
                        gross_return_pct=gross_return,
                        total_cost_pct=total_cost,
                        max_favourable_excursion_pct=mfe,
                        max_adverse_excursion_pct=mae,
                        atr_pct=float(candidate.atr_pct or 0.0),
                        liquidity_bucket=liquidity_bucket(stock, signal_index),
                    ))
    return trades


def summarize_trades(trades: Sequence[BacktestTrade]) -> Dict[str, float]:
    if not trades:
        return {
            "trades": 0,
            "win_rate_pct": 0.0,
            "average_return_pct": 0.0,
            "average_gain_pct": 0.0,
            "average_loss_pct": 0.0,
            "profit_factor": 0.0,
            "expectancy_pct": 0.0,
            "maximum_drawdown_pct": 0.0,
            "average_holding_sessions": 0.0,
            "target_rate_pct": 0.0,
            "stop_rate_pct": 0.0,
        }

    ordered = sorted(trades, key=lambda t: (t.entry_date, t.symbol))
    returns = np.array([float(t.return_pct) for t in ordered], dtype=float)
    gains = returns[returns > 0]
    losses = returns[returns <= 0]
    gross_profit = float(gains.sum()) if len(gains) else 0.0
    gross_loss = abs(float(losses.sum())) if len(losses) else 0.0
    profit_factor = gross_profit / gross_loss if gross_loss > 0 else (float("inf") if gross_profit > 0 else 0.0)

    equity = np.cumprod(1 + returns / 100)
    running_peak = np.maximum.accumulate(equity)
    drawdowns = (equity / running_peak - 1) * 100

    return {
        "trades": int(len(ordered)),
        "win_rate_pct": float((returns > 0).mean() * 100),
        "average_return_pct": float(returns.mean()),
        "average_gain_pct": float(gains.mean()) if len(gains) else 0.0,
        "average_loss_pct": float(losses.mean()) if len(losses) else 0.0,
        "profit_factor": float(profit_factor),
        "expectancy_pct": float(returns.mean()),
        "maximum_drawdown_pct": float(drawdowns.min()) if len(drawdowns) else 0.0,
        "average_holding_sessions": float(np.mean([t.holding_sessions for t in ordered])),
        "target_rate_pct": float(sum(t.result == "TARGET" for t in ordered) / len(ordered) * 100),
        "stop_rate_pct": float(sum(t.result == "STOP" for t in ordered) / len(ordered) * 100),
        "average_mfe_pct": float(np.mean([t.max_favourable_excursion_pct for t in ordered])),
        "average_mae_pct": float(np.mean([t.max_adverse_excursion_pct for t in ordered])),
    }


def trades_to_frame(trades: Sequence[BacktestTrade]) -> pd.DataFrame:
    return pd.DataFrame([asdict(trade) for trade in trades])


def grouped_summary(trades: Sequence[BacktestTrade], group_fields: Sequence[str]) -> pd.DataFrame:
    frame = trades_to_frame(trades)
    if frame.empty:
        columns = list(group_fields) + list(summarize_trades([]).keys())
        return pd.DataFrame(columns=columns)

    rows: List[dict] = []
    for keys, group in frame.groupby(list(group_fields), dropna=False):
        if not isinstance(keys, tuple):
            keys = (keys,)
        group_trades = [BacktestTrade(**record) for record in group.to_dict(orient="records")]
        row = dict(zip(group_fields, keys))
        row.update(summarize_trades(group_trades))
        rows.append(row)
    return pd.DataFrame(rows)
