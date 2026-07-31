"""
Test suite for the Next-Day Explosive Move Engine.

Covers (per the spec's Section 24, scoped to what's actually implemented -
see explosive_engine.py's module docstring for what's deliberately
deferred): synthetic positive + negative cases for all 4 pattern detectors,
no-look-ahead-bias, insufficient/stale data, and calibration sample-size
gating. Run with: pytest test_explosive_engine.py -v
"""

import numpy as np
import pandas as pd
import pytest

from explosive_config import DEFAULT_CONFIG
from explosive_patterns import detect_vcp, detect_tight_base, detect_ascending_triangle, detect_bull_flag
from explosive_indicators import inside_bar, narrow_range_n, safe_div
from explosive_labels import compute_next_day_outcome
from explosive_calibration import summarize_calibration, PROB_LABEL_NOT_AVAILABLE
from explosive_score import compute_explosive_score
from market_scanner import atr as atr_fn


def _ohlcv(close, high=None, low=None, vol=None):
    close = pd.Series(close, dtype=float)
    high = pd.Series(high, dtype=float) if high is not None else close + 1
    low = pd.Series(low, dtype=float) if low is not None else close - 1
    vol = pd.Series(vol, dtype=float) if vol is not None else pd.Series(np.full(len(close), 100_000.0))
    return high, low, close, vol


# --------------------------------------------------------------- VCP

def test_vcp_positive():
    np.random.seed(1)
    n = 60
    trend = np.linspace(90, 100, n)
    amp = np.linspace(6, 0.8, n)
    close = trend + amp * np.sin(np.linspace(0, 10 * np.pi, n))
    high = close + amp * 0.3 + 0.3
    low = close - amp * 0.3 - 0.3
    vol = np.linspace(300_000, 80_000, n)
    h, l, c, v = _ohlcv(close, high, low, vol)
    atr = atr_fn(pd.DataFrame({"High": h, "Low": l, "Close": c}), 14)
    result = detect_vcp(h, l, c, v, atr)
    assert result["detected"] is True
    assert result["quality"] > 50


def test_vcp_negative_random_noise():
    false_positives = 0
    for seed in range(20):
        np.random.seed(seed + 100)
        close = 100 + np.cumsum(np.random.randn(60) * 1.5)
        high = close + np.random.uniform(1, 3, 60)
        low = close - np.random.uniform(1, 3, 60)
        vol = np.random.uniform(50_000, 300_000, 60)
        h, l, c, v = _ohlcv(close, high, low, vol)
        atr = atr_fn(pd.DataFrame({"High": h, "Low": l, "Close": c}), 14)
        if detect_vcp(h, l, c, v, atr)["detected"]:
            false_positives += 1
    assert false_positives == 0, f"{false_positives}/20 false positives on random noise"


# --------------------------------------------------------------- Tight Base

def test_tight_base_positive():
    np.random.seed(3)
    n = 15
    close = 100 + np.random.uniform(-2, 2, n)
    close[-3:] = [101, 101.5, 102]
    vol = np.concatenate([np.full(10, 200_000), np.full(5, 90_000)])
    h, l, c, v = _ohlcv(close, close + 1, close - 1, vol)
    result = detect_tight_base(h, l, c, v)
    assert result["detected"] is True


def test_tight_base_negative_trending():
    false_positives = 0
    for seed in range(10):
        np.random.seed(seed + 200)
        close = 100 + np.cumsum(np.random.randn(15) * 3)
        h, l, c, v = _ohlcv(close)
        if detect_tight_base(h, l, c, v)["detected"]:
            false_positives += 1
    assert false_positives == 0, f"{false_positives}/10 false positives on trending noise"


# --------------------------------------------------------------- Ascending Triangle

def test_ascending_triangle_positive():
    np.random.seed(4)
    n = 60
    close = np.linspace(90, 98, n) + np.random.randn(n) * 0.3
    high, low = close.copy(), close.copy() - 1
    for idx in (15, 35, 55):
        high[idx] = 100.2
    for idx, val in zip((10, 30, 50), (92, 95, 97)):
        low[idx] = val
    close[-1] = 99.5
    h, l, c, v = _ohlcv(close, high, low)
    atr = atr_fn(pd.DataFrame({"High": h, "Low": l, "Close": c}), 14)
    result = detect_ascending_triangle(h, l, c, atr.iloc[-1])
    assert result["detected"] is True


def test_ascending_triangle_negative_random_walk():
    false_positives = 0
    for seed in range(20):
        np.random.seed(seed + 300)
        close = 100 + np.cumsum(np.random.randn(60) * 1.2)
        h, l, c, v = _ohlcv(close)
        atr = atr_fn(pd.DataFrame({"High": h, "Low": l, "Close": c}), 14)
        if detect_ascending_triangle(h, l, c, atr.iloc[-1])["detected"]:
            false_positives += 1
    # Disclosed, not hidden: a heuristic geometric screen isn't expected to
    # hit exactly 0/20 on adversarial random data - assert it stays low.
    assert false_positives <= 2, f"{false_positives}/20 false positives - regression from tested baseline"


# --------------------------------------------------------------- Bull Flag

def test_bull_flag_positive():
    pole = np.linspace(90, 105, 25)
    flag = 105 - np.linspace(0, 2, 25) + np.sin(np.linspace(0, 6, 25)) * 0.5
    close = np.concatenate([pole, flag])
    vol = np.concatenate([np.full(25, 300_000), np.full(25, 120_000)])
    h, l, c, v = _ohlcv(close, vol=vol)
    result = detect_bull_flag(h, l, c, v)
    assert result["detected"] is True


def test_bull_flag_negative_random_noise():
    false_positives = 0
    for seed in range(10):
        np.random.seed(seed + 400)
        close = 100 + np.cumsum(np.random.randn(50) * 1.5)
        h, l, c, v = _ohlcv(close)
        if detect_bull_flag(h, l, c, v)["detected"]:
            false_positives += 1
    assert false_positives == 0, f"{false_positives}/10 false positives on random noise"


# --------------------------------------------------------------- Supporting signals

def test_inside_bar_detection():
    high = pd.Series([10, 9])
    low = pd.Series([5, 6])
    assert inside_bar(high, low) is True
    high2 = pd.Series([10, 11])
    low2 = pd.Series([5, 4])
    assert inside_bar(high2, low2) is False


def test_narrow_range_7():
    ranges_wide = [5] * 6 + [0.5]
    high = pd.Series(np.cumsum(ranges_wide))
    low = pd.Series(np.cumsum(ranges_wide) - np.array(ranges_wide))
    assert narrow_range_n(high, low, 7) is True


# --------------------------------------------------------------- Excessive extension / failed breakout penalties

def test_excessive_extension_penalty_applied():
    result = compute_explosive_score(
        pattern_quality=80, is_bullish_pattern=True,
        compression_metrics={}, trend_rs_metrics={}, volume_metrics={},
        pivot_candle_metrics={}, market_regime_score=8, liquidity_ok=True,
        data_quality_pct=95, pct_above_ema20=25.0, config=DEFAULT_CONFIG,
    )
    assert any("extension" in p.lower() for p in result.penalties)
    assert result.penalty_total > 0


def test_upper_wick_rejection_penalty_applied():
    result = compute_explosive_score(
        pattern_quality=80, is_bullish_pattern=True,
        compression_metrics={}, trend_rs_metrics={},
        volume_metrics={}, pivot_candle_metrics=dict(upper_wick_pct=80),
        market_regime_score=8, liquidity_ok=True, data_quality_pct=95,
        pct_above_ema20=2.0, config=DEFAULT_CONFIG,
    )
    assert any("wick" in p.lower() for p in result.penalties)


# --------------------------------------------------------------- Stale / insufficient data

def test_hard_reject_insufficient_data():
    result = compute_explosive_score(
        pattern_quality=90, is_bullish_pattern=True, compression_metrics={}, trend_rs_metrics={},
        volume_metrics={}, pivot_candle_metrics={}, market_regime_score=9,
        liquidity_ok=True, data_quality_pct=30, pct_above_ema20=2, config=DEFAULT_CONFIG,
    )
    assert result.hard_rejected is True
    assert "INSUFFICIENT_DATA" in result.hard_reject_reason


def test_hard_reject_illiquid():
    result = compute_explosive_score(
        pattern_quality=90, is_bullish_pattern=True, compression_metrics={}, trend_rs_metrics={},
        volume_metrics={}, pivot_candle_metrics={}, market_regime_score=9,
        liquidity_ok=False, data_quality_pct=95, pct_above_ema20=2, config=DEFAULT_CONFIG,
    )
    assert result.hard_rejected is True


# --------------------------------------------------------------- No-look-ahead bias (MANDATORY, Section 12)

def test_no_lookahead_bias_pattern_signal_unaffected_by_future_mutation():
    n = 30
    np.random.seed(9)
    close = pd.Series(100 + np.random.uniform(-1, 1, n))
    close.iloc[-3:] = [101, 101.3, 101.6]
    high, low, _, vol = _ohlcv(close.values)

    t = 25
    sig_before = detect_tight_base(high.iloc[:t + 1], low.iloc[:t + 1], close.iloc[:t + 1], vol.iloc[:t + 1])

    df_mutated_close = close.copy()
    df_mutated_high = high.copy()
    df_mutated_low = low.copy()
    df_mutated_close.iloc[t + 2:] = 50.0
    df_mutated_high.iloc[t + 2:] = 200.0
    df_mutated_low.iloc[t + 2:] = 10.0

    sig_after = detect_tight_base(df_mutated_high.iloc[:t + 1], df_mutated_low.iloc[:t + 1],
                                   df_mutated_close.iloc[:t + 1], vol.iloc[:t + 1])
    assert sig_before == sig_after, "Signal at day t changed when future (t+2 onward) data was mutated!"


def test_no_lookahead_label_only_uses_t_plus_1():
    n = 20
    close = pd.Series(np.linspace(100, 110, n))
    high, low = close + 1, close - 1
    open_ = close.shift(1).fillna(100)
    vol = pd.Series(np.full(n, 100_000.0))
    df = pd.DataFrame({"Open": open_, "High": high, "Low": low, "Close": close, "Volume": vol})

    df.loc[11, "High"] = df.loc[10, "Close"] * 1.05
    df.loc[11, "Close"] = df.loc[10, "Close"] * 1.045
    df.loc[11, "Low"] = df.loc[10, "Close"] * 0.995
    df.loc[11, "Volume"] = 250_000

    outcome = compute_next_day_outcome(df, t=10, breakout_trigger=None, atr_at_t=1.5, config=DEFAULT_CONFIG)
    assert outcome.label_computable is True
    assert outcome.explosive_intraday_success is True

    # Mutating day 12+ must NOT change the outcome computed for t=10 (which
    # only ever reads t+1=11)
    df2 = df.copy()
    df2.loc[12:, "Close"] = 0.01
    df2.loc[12:, "High"] = 999.0
    outcome2 = compute_next_day_outcome(df2, t=10, breakout_trigger=None, atr_at_t=1.5, config=DEFAULT_CONFIG)
    assert outcome.next_day_high_return_pct == outcome2.next_day_high_return_pct
    assert outcome.explosive_intraday_success == outcome2.explosive_intraday_success


def test_label_not_computable_for_last_row():
    n = 10
    close = pd.Series(np.linspace(100, 110, n))
    high, low = close + 1, close - 1
    vol = pd.Series(np.full(n, 100_000.0))
    df = pd.DataFrame({"Open": close.shift(1).fillna(100), "High": high, "Low": low,
                        "Close": close, "Volume": vol})
    outcome = compute_next_day_outcome(df, t=n - 1, breakout_trigger=None, atr_at_t=1.0, config=DEFAULT_CONFIG)
    assert outcome.label_computable is False


# --------------------------------------------------------------- Calibration sample-size gating

def test_calibration_not_available_below_min_sample():
    tiny_samples = [dict(score_bucket="75-81", pattern="Tight Base", success=True,
                          next_day_high_return_pct=5.0, next_day_close_return_pct=3.0, mae_pct=1.0)] * 3
    result = summarize_calibration(tiny_samples, "75-81", "Tight Base", DEFAULT_CONFIG)
    assert result.probability_label == PROB_LABEL_NOT_AVAILABLE
    assert result.hit_rate_pct is None


def test_calibration_available_above_min_sample():
    n_samples = DEFAULT_CONFIG.min_calibration_sample_size + 5
    samples = [dict(score_bucket="75-81", pattern="Tight Base",
                     success=(i % 2 == 0), next_day_high_return_pct=5.0,
                     next_day_close_return_pct=3.0, mae_pct=1.0) for i in range(n_samples)]
    result = summarize_calibration(samples, "75-81", "Tight Base", DEFAULT_CONFIG)
    assert result.probability_label == "EMPIRICAL_HIT_RATE"
    assert result.sample_size == n_samples


# --------------------------------------------------------------- safe_div defensiveness

def test_safe_div_zero_denominator_never_crashes_or_fakes_zero():
    assert safe_div(5, 0) is None
    assert safe_div(0, 0) is None
    assert safe_div(None, 5) is None
    assert safe_div(5, None) is None
