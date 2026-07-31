"""
Phase 1 historical calibration: EMPIRICAL_HIT_RATE only (Section 13).

WHAT THIS ACTUALLY IS: for a single stock's OWN fetched history (up to
`calibration_years`), walks day-by-day, computes what the pattern/score
WOULD have been at each historical day using ONLY data through that day,
computes the real next-day outcome (explosive_labels.py), and aggregates
hit rates by score bucket. This is genuinely real - not fabricated - but
it's a per-stock, single-symbol sample, which is a MUCH smaller and noisier
sample than a proper cross-sectional multi-year study across the whole
universe. The confidence label reflects that honestly.

PHASE 2 (ML calibration, Section 13) IS NOT IMPLEMENTED. It requires
pooled cross-sectional training data with walk-forward splits - genuinely
more infrastructure than a per-symbol calibration can provide, and I won't
fake a probability that looks ML-calibrated when it isn't. See the "Known
limitations" section of the response for the full reasoning.
"""

from dataclasses import dataclass, field
from typing import Optional, List
import numpy as np
import pandas as pd

from explosive_labels import compute_next_day_outcome
from explosive_patterns import detect_all_patterns
from market_scanner import atr as atr_fn


PROB_LABEL_CALIBRATED = "CALIBRATED_PROBABILITY"   # never produced by this Phase-1-only module
PROB_LABEL_EMPIRICAL = "EMPIRICAL_HIT_RATE"
PROB_LABEL_NOT_AVAILABLE = "NOT_AVAILABLE"


@dataclass
class CalibrationResult:
    probability_label: str          # one of the three constants above
    hit_rate_pct: Optional[float]
    sample_size: int
    avg_next_day_high_return_pct: Optional[float]
    median_next_day_close_return_pct: Optional[float]
    avg_max_adverse_excursion_pct: Optional[float]
    failure_rate_pct: Optional[float]


def _score_bucket(score: float) -> str:
    if score >= 90: return "90-100"
    if score >= 82: return "82-89"
    if score >= 75: return "75-81"
    if score >= 68: return "68-74"
    return "below-68"


def run_symbol_calibration(df: pd.DataFrame, config, is_smallcap: bool = False,
                            min_bar: int = 60) -> List[dict]:
    """Walks the FULL history in df once, producing one (score_bucket,
    pattern, outcome) sample per valid historical day. `df` must already be
    the calibration-period history (e.g. 3-5 years), reset-indexed 0..N-1.
    Every day t only ever sees df.iloc[:t+1] for its own signal - the
    outcome for t comes from compute_next_day_outcome(df, t, ...), which
    itself only reads t+1 (see explosive_labels.py's own invariant)."""
    from explosive_score import compute_explosive_score
    from explosive_indicators import (ema, bollinger_width_percentile, range_compression_ratio,
                                       volume_dry_up_ratio, up_down_volume_ratio, obv_slope, candle_quality)

    samples = []
    n = len(df)
    if n < min_bar + 2:
        return samples

    atr_full = atr_fn(df, 14)

    for t in range(min_bar, n - 1):   # -1 because we need t+1 to exist for the label
        close_slice = df["Close"].iloc[:t + 1]
        high_slice = df["High"].iloc[:t + 1]
        low_slice = df["Low"].iloc[:t + 1]
        vol_slice = df["Volume"].iloc[:t + 1]
        atr_slice = atr_full.iloc[:t + 1]

        if close_slice.isna().any() or atr_slice.iloc[-1] is None or pd.isna(atr_slice.iloc[-1]):
            continue

        pattern_result = detect_all_patterns(high_slice, low_slice, close_slice, vol_slice, atr_slice, config)
        primary = pattern_result["primary_result"]
        if not primary["detected"]:
            continue   # calibration only tracks days where SOME pattern fired

        ema20 = ema(close_slice, 20)
        pct_above_ema20 = ((close_slice.iloc[-1] - ema20.iloc[-1]) / ema20.iloc[-1] * 100
                            if ema20.iloc[-1] else None)
        bb_pctile = bollinger_width_percentile(close_slice)
        range_ratio = range_compression_ratio(high_slice, low_slice)
        dry_up = volume_dry_up_ratio(vol_slice)
        ud_ratio = up_down_volume_ratio(close_slice, vol_slice)
        obv = obv_slope(close_slice, vol_slice)
        cq = candle_quality(df["Open"].iloc[t] if "Open" in df.columns else close_slice.iloc[-1],
                             high_slice.iloc[-1], low_slice.iloc[-1], close_slice.iloc[-1])
        pivot = primary["pivot"]
        dist_to_pivot = ((pivot - close_slice.iloc[-1]) / pivot * 100) if pivot else None

        score_result = compute_explosive_score(
            pattern_quality=primary["quality"], is_bullish_pattern=True,
            compression_metrics=dict(bb_width_percentile=bb_pctile, range_ratio_5_20=range_ratio),
            trend_rs_metrics=dict(ma_alignment="BULLISH_ALIGNED", rs_1m=None, rs_3m=None,
                                   ema20_slope=(1 if pct_above_ema20 and pct_above_ema20 > 0 else -1)),
            volume_metrics=dict(vol_dry_up_ratio=dry_up, breakout_vol_ratio=None,
                                 obv_slope=obv, up_down_vol_ratio=ud_ratio),
            pivot_candle_metrics=dict(distance_to_pivot_pct=dist_to_pivot, clv=cq.get("clv"),
                                       upper_wick_pct=cq.get("upper_wick_pct")),
            market_regime_score=6.0,  # neutral placeholder - true historical regime not reconstructed per-day here
            liquidity_ok=True, data_quality_pct=100.0,
            pct_above_ema20=pct_above_ema20, config=config,
        )
        if score_result.hard_rejected:
            continue

        atr_at_t = atr_slice.iloc[-1]
        outcome = compute_next_day_outcome(df, t, breakout_trigger=pivot, atr_at_t=atr_at_t,
                                            config=config, is_smallcap=is_smallcap)
        if not outcome.label_computable:
            continue

        samples.append(dict(
            score_bucket=_score_bucket(score_result.final_score), pattern=pattern_result["primary_pattern"],
            score=score_result.final_score, success=outcome.explosive_intraday_success,
            next_day_high_return_pct=outcome.next_day_high_return_pct,
            next_day_close_return_pct=outcome.next_day_close_return_pct,
            mae_pct=outcome.next_day_max_adverse_excursion_pct,
        ))

    return samples


def summarize_calibration(samples: List[dict], current_bucket: str, current_pattern: str,
                           config) -> CalibrationResult:
    """Filters the symbol's own historical samples down to the SAME score
    bucket + pattern as today's live signal, and reports the empirical hit
    rate for that specific combination - never for the whole sample
    indiscriminately, since a stock's 90+ bucket and 68-74 bucket behave
    very differently."""
    matching = [s for s in samples if s["score_bucket"] == current_bucket and s["pattern"] == current_pattern]
    n = len(matching)
    if n < config.min_calibration_sample_size:
        return CalibrationResult(PROB_LABEL_NOT_AVAILABLE, None, n, None, None, None, None)

    successes = [s for s in matching if s["success"]]
    hit_rate = len(successes) / n * 100
    avg_high = float(np.mean([s["next_day_high_return_pct"] for s in matching if s["next_day_high_return_pct"] is not None]))
    med_close = float(np.median([s["next_day_close_return_pct"] for s in matching if s["next_day_close_return_pct"] is not None]))
    avg_mae = float(np.mean([s["mae_pct"] for s in matching if s["mae_pct"] is not None]))
    failure_rate = 100 - hit_rate

    return CalibrationResult(
        PROB_LABEL_EMPIRICAL, round(hit_rate, 1), n,
        round(avg_high, 2), round(med_close, 2), round(avg_mae, 2), round(failure_rate, 1),
    )
