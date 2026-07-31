"""
Explosive Setup Score: explainable 0-100 combining pattern quality,
volatility compression, trend/RS, volume, pivot/candle quality, market
regime, and liquidity/data-quality - each as ONE component, per Section 8's
explicit instruction not to double-count correlated signals (ATR, Bollinger
width, and range compression are correlated -> they form ONE compression
component, not three).

Also implements the hard-rejection/penalty logic (Section 9) and setup-stage
classification (Section 10).
"""

from dataclasses import dataclass, field
from typing import Optional, List
import numpy as np

from explosive_indicators import safe_div

STAGE_PRE_BREAKOUT = "PRE_BREAKOUT"
STAGE_BREAKOUT_DAY = "BREAKOUT_DAY"
STAGE_BREAKOUT_RETEST = "BREAKOUT_RETEST"
STAGE_EARLY_EXPANSION = "EARLY_EXPANSION"
STAGE_EXTENDED = "EXTENDED"
STAGE_FAILED_SETUP = "FAILED_SETUP"

BAND_A_PLUS = "A+ EXCEPTIONAL SETUP"
BAND_A = "A HIGH-CONVICTION WATCH"
BAND_B_PLUS = "B+ STRONG WATCHLIST"
BAND_B = "B CONDITIONAL SETUP"
BAND_EXCLUDE = "DO NOT INCLUDE IN PRIMARY LIST"


@dataclass
class ScoreBreakdown:
    pattern_score: float
    compression_score: float
    trend_rs_score: float
    volume_score: float
    pivot_candle_score: float
    market_score: float
    liquidity_score: float
    raw_total: float
    penalties: List[str] = field(default_factory=list)
    penalty_total: float = 0.0
    final_score: float = 0.0
    hard_rejected: bool = False
    hard_reject_reason: Optional[str] = None
    setup_stage: str = STAGE_FAILED_SETUP
    band: str = BAND_EXCLUDE


def _extension_penalty(pct_above_ema20: Optional[float], config) -> tuple:
    if pct_above_ema20 is None or pct_above_ema20 <= config.ema20_extension_penalty_start_pct:
        return 0.0, None
    span = config.ema20_extension_penalty_max_pct - config.ema20_extension_penalty_start_pct
    frac = min(1.0, (pct_above_ema20 - config.ema20_extension_penalty_start_pct) / span) if span > 0 else 1.0
    penalty = frac * config.extension_penalty_max
    return penalty, f"Excessive extension: {pct_above_ema20:.1f}% above EMA20 (-{penalty:.1f})"


def _upper_wick_penalty(upper_wick_pct: Optional[float], config) -> tuple:
    if upper_wick_pct is None or upper_wick_pct <= config.upper_wick_penalty_pct_threshold:
        return 0.0, None
    frac = min(1.0, (upper_wick_pct - config.upper_wick_penalty_pct_threshold) / 40.0)
    penalty = frac * config.upper_wick_penalty_max
    return penalty, f"Bearish upper-wick supply: {upper_wick_pct:.0f}% of range (-{penalty:.1f})"


def _distribution_penalty(up_down_vol_ratio: Optional[float], config) -> tuple:
    if up_down_vol_ratio is None or up_down_vol_ratio >= 1.0 / config.distribution_vol_ratio_threshold:
        return 0.0, None
    penalty = config.distribution_penalty_max
    return penalty, f"Distribution volume: down-day volume exceeds up-day volume (-{penalty:.1f})"


def compute_explosive_score(
    pattern_quality: float, is_bullish_pattern: bool,
    compression_metrics: dict,     # atr_pct, bb_width_percentile, range_ratio_5_20
    trend_rs_metrics: dict,        # ma_alignment, rs_1m, rs_3m, ema20_slope, sma50_slope
    volume_metrics: dict,          # vol_dry_up_ratio, breakout_vol_ratio, obv_slope, up_down_vol_ratio
    pivot_candle_metrics: dict,    # distance_to_pivot_pct, clv, upper_wick_pct
    market_regime_score: float,    # 0-10 from explosive_market_regime.classify_regime
    liquidity_ok: bool, data_quality_pct: float,
    pct_above_ema20: Optional[float],
    config,
) -> ScoreBreakdown:

    # --- Hard rejections first (Section 9) - short-circuit everything else ---
    if data_quality_pct < 50.0:
        return ScoreBreakdown(0, 0, 0, 0, 0, 0, 0, 0, hard_rejected=True,
                               hard_reject_reason=f"Data completeness only {data_quality_pct:.0f}% - INSUFFICIENT_DATA",
                               setup_stage=STAGE_FAILED_SETUP, band=BAND_EXCLUDE)
    if not liquidity_ok:
        return ScoreBreakdown(0, 0, 0, 0, 0, 0, 0, 0, hard_rejected=True,
                               hard_reject_reason="Fails liquidity filter (traded value/volume/price floor)",
                               setup_stage=STAGE_FAILED_SETUP, band=BAND_EXCLUDE)

    # --- A. Pattern quality (25 pts) ---
    pattern_score = (pattern_quality / 100.0) * config.weight_pattern_quality if is_bullish_pattern else 0.0

    # --- B. Compression (15 pts) - ONE combined component from correlated measures ---
    bb_pctile = compression_metrics.get("bb_width_percentile")
    range_ratio = compression_metrics.get("range_ratio_5_20")
    comp_signals = []
    if bb_pctile is not None:
        comp_signals.append(max(0.0, (30 - bb_pctile) / 30))   # low percentile = more compressed = higher score
    if range_ratio is not None:
        comp_signals.append(max(0.0, min(1.0, (1.0 - range_ratio) / 0.5)))
    compression_score = (np.mean(comp_signals) * config.weight_compression) if comp_signals else 0.0

    # --- C. Trend & relative strength (20 pts) ---
    trs_signals = []
    if trend_rs_metrics.get("ma_alignment") == "BULLISH_ALIGNED":
        trs_signals.append(1.0)
    elif trend_rs_metrics.get("ma_alignment") == "ALIGNED_BUT_PRICE_BELOW":
        trs_signals.append(0.4)
    else:
        trs_signals.append(0.0)
    rs_1m, rs_3m = trend_rs_metrics.get("rs_1m"), trend_rs_metrics.get("rs_3m")
    if rs_1m is not None:
        trs_signals.append(1.0 if rs_1m > 0 else 0.0)
    if rs_3m is not None:
        trs_signals.append(1.0 if rs_3m > 0 else 0.0)
    ema20_slope = trend_rs_metrics.get("ema20_slope")
    if ema20_slope is not None:
        trs_signals.append(1.0 if ema20_slope > 0 else 0.0)
    trend_rs_score = (np.mean(trs_signals) * config.weight_trend_rs) if trs_signals else 0.0

    # --- D. Volume & accumulation (15 pts) ---
    vol_signals = []
    dry_up = volume_metrics.get("vol_dry_up_ratio")
    if dry_up is not None:
        vol_signals.append(1.0 if dry_up < config.volume_dry_up_ratio_threshold else 0.3)
    breakout_vol = volume_metrics.get("breakout_vol_ratio")
    if breakout_vol is not None:
        vol_signals.append(1.0 if breakout_vol >= config.breakout_volume_ratio_threshold else 0.3)
    obv = volume_metrics.get("obv_slope")
    if obv is not None:
        vol_signals.append(1.0 if obv > 0 else 0.2)
    volume_score = (np.mean(vol_signals) * config.weight_volume) if vol_signals else 0.0

    # --- E. Pivot proximity & candle quality (10 pts) ---
    pc_signals = []
    dist_to_pivot = pivot_candle_metrics.get("distance_to_pivot_pct")
    if dist_to_pivot is not None:
        if 0 <= dist_to_pivot <= config.pivot_proximity_pct:
            pc_signals.append(1.0)
        elif dist_to_pivot < 0 and abs(dist_to_pivot) <= config.pivot_extension_max_pct:
            pc_signals.append(0.8)   # just broken out, not yet extended
        else:
            pc_signals.append(0.1)
    clv = pivot_candle_metrics.get("clv")
    if clv is not None:
        pc_signals.append(clv)   # already 0-1
    pivot_candle_score = (np.mean(pc_signals) * config.weight_pivot_candle) if pc_signals else 0.0

    # --- F. Market/sector regime (10 pts) ---
    market_score = (market_regime_score / 10.0) * config.weight_market_regime

    # --- G. Liquidity & data quality (5 pts) ---
    liquidity_score = (data_quality_pct / 100.0) * config.weight_liquidity_data_quality

    raw_total = (pattern_score + compression_score + trend_rs_score + volume_score +
                 pivot_candle_score + market_score + liquidity_score)

    # --- Penalties (Section 9) ---
    penalties = []
    penalty_total = 0.0
    ext_pen, ext_reason = _extension_penalty(pct_above_ema20, config)
    if ext_reason: penalties.append(ext_reason); penalty_total += ext_pen
    wick_pen, wick_reason = _upper_wick_penalty(pivot_candle_metrics.get("upper_wick_pct"), config)
    if wick_reason: penalties.append(wick_reason); penalty_total += wick_pen
    dist_pen, dist_reason = _distribution_penalty(volume_metrics.get("up_down_vol_ratio"), config)
    if dist_reason: penalties.append(dist_reason); penalty_total += dist_pen
    if market_regime_score <= 2.0:
        wm_pen = config.weak_market_penalty_max
        penalties.append(f"Weak/risk-off market regime (-{wm_pen:.1f})")
        penalty_total += wm_pen

    final_score = max(0.0, min(100.0, raw_total - penalty_total))

    # --- Setup stage classification (Section 10) ---
    if dist_to_pivot is not None and 0 <= dist_to_pivot <= config.pivot_proximity_pct:
        stage = STAGE_PRE_BREAKOUT
    elif dist_to_pivot is not None and -config.breakout_trigger_buffer_pct <= dist_to_pivot < 0:
        stage = STAGE_BREAKOUT_DAY
    elif dist_to_pivot is not None and -config.pivot_extension_max_pct <= dist_to_pivot < -config.breakout_trigger_buffer_pct:
        stage = STAGE_EARLY_EXPANSION
    elif dist_to_pivot is not None and dist_to_pivot < -config.pivot_extension_max_pct:
        stage = STAGE_EXTENDED
    else:
        stage = STAGE_FAILED_SETUP

    # --- Final band (Section 18) ---
    if final_score >= config.band_a_plus_min:
        band = BAND_A_PLUS
    elif final_score >= config.band_a_min:
        band = BAND_A
    elif final_score >= config.band_b_plus_min:
        band = BAND_B_PLUS
    elif final_score >= config.band_b_min:
        band = BAND_B
    else:
        band = BAND_EXCLUDE

    return ScoreBreakdown(
        pattern_score=round(pattern_score, 2), compression_score=round(compression_score, 2),
        trend_rs_score=round(trend_rs_score, 2), volume_score=round(volume_score, 2),
        pivot_candle_score=round(pivot_candle_score, 2), market_score=round(market_score, 2),
        liquidity_score=round(liquidity_score, 2), raw_total=round(raw_total, 2),
        penalties=penalties, penalty_total=round(penalty_total, 2),
        final_score=round(final_score, 2), hard_rejected=False, hard_reject_reason=None,
        setup_stage=stage, band=band,
    )
