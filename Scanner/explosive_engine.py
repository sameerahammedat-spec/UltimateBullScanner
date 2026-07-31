"""
Next-Day Explosive Move Engine - orchestrator.

SCOPE HONESTLY STATED (read this before trusting the output):
- 4 pattern detectors implemented and tested against synthetic positive/
  negative cases (VCP, Tight Base, Ascending Triangle, Bull Flag), not the
  full 9 from the original spec. Shakeout & Reclaim, Breakout & Retest, and
  Rounding Base are NOT implemented - rather than ship shallow, untested
  versions of those, they're left out entirely.
- Calibration is Phase 1 ONLY (per-symbol empirical bucket hit-rate from
  that stock's own history). Phase 2 (pooled cross-sectional ML
  calibration with walk-forward validation) is NOT implemented - it needs
  materially more data infrastructure than a single-symbol calibration can
  provide, and faking that scaffolding would produce a probability that
  LOOKS rigorously validated when it isn't.
- No circuit-band/price-band risk (no reliable free data source for BSE/
  NSE price bands) and no sector-index relative strength (same reason,
  no reliable free sector-index mapping/data).
- Chart image generation (Section 21) is not built - it was explicitly
  optional in the spec.

Everything that IS here is real, tested against synthetic data, and free
of look-ahead bias (explicit test in test_explosive_engine.py).
"""

from dataclasses import dataclass, field
from typing import Optional, List, Dict
from datetime import datetime, date
import numpy as np
import pandas as pd

from explosive_config import ExplosiveConfig, DEFAULT_CONFIG
from explosive_indicators import (ema, sma, series_slope_pct_per_day, ma_alignment,
                                    bollinger_width_percentile, range_compression_ratio,
                                    volume_dry_up_ratio, up_down_volume_ratio, obv_slope,
                                    candle_quality, safe_div, atr_pct as atr_pct_fn,
                                    distance_from_high)
from explosive_patterns import detect_all_patterns
from explosive_score import compute_explosive_score, BAND_EXCLUDE
from explosive_swings import find_swings, nearest_resistance_above
from explosive_calibration import (run_symbol_calibration, summarize_calibration,
                                     PROB_LABEL_NOT_AVAILABLE)
from explosive_labels import compute_next_day_outcome
from market_scanner import atr as atr_fn, adx as adx_fn


@dataclass
class ExplosiveCandidate:
    symbol: str
    name: str
    exchange: str
    scanner_sources: List[str]
    sector: str
    close: float
    primary_pattern: str
    secondary_pattern: Optional[str]
    setup_stage: str
    setup_duration: Optional[int]
    pivot: Optional[float]
    distance_to_pivot_pct: Optional[float]
    breakout_trigger: Optional[float]
    setup_low: Optional[float]
    invalidation: Optional[float]
    risk_to_invalidation_pct: Optional[float]
    atr_pct: Optional[float]
    range_compression_5_20: Optional[float]
    bb_width_percentile: Optional[float]
    volume_dry_up_ratio: Optional[float]
    relative_volume: Optional[float]
    close_location_value: Optional[float]
    rs_1m: Optional[float]
    rs_3m: Optional[float]
    market_regime: str
    circuit_risk: str
    pattern_score: float
    compression_score: float
    trend_rs_score: float
    volume_score: float
    market_score: float
    penalty_total: float
    final_score: float
    probability_label: str
    hit_rate_pct: Optional[float]
    sample_size: int
    confidence: str
    data_quality_pct: float
    data_quality_status: str
    positive_reasons: List[str]
    main_risks: List[str]
    invalidation_reason: str
    final_verdict: str
    rejected: bool = False
    rejection_reason: Optional[str] = None


def data_quality_check(df: pd.DataFrame, config: ExplosiveConfig, most_recent_expected: date) -> dict:
    """Section 3: completeness %, usable sessions, most recent date, stale flag."""
    n = len(df)
    completeness_pct = float(df["Close"].notna().mean() * 100) if n else 0.0
    most_recent_date = df.index[-1].date() if hasattr(df.index[-1], "date") else None
    stale = False
    if most_recent_date is not None:
        stale = (most_recent_expected - most_recent_date).days > config.stale_data_max_days

    if n < config.min_history_acceptable:
        status = "INSUFFICIENT_DATA"
    elif stale:
        status = "STALE"
    elif n < config.min_history_preferred:
        status = "ACCEPTABLE_SHORT_HISTORY"
    else:
        status = "GOOD"

    return dict(completeness_pct=completeness_pct, usable_sessions=n,
                most_recent_date=most_recent_date, stale=stale, status=status)


def build_explanation(pattern_result: dict, pattern_name: str, score_breakdown, metrics: dict) -> tuple:
    """Deterministic explanation from actual measured values - NEVER an LLM
    guess (Section 19). Returns (positive_reasons: List[str], risks: List[str])."""
    positives = []
    risks = list(score_breakdown.penalties)  # penalties ARE the measured risks

    if pattern_result.get("detected"):
        dur = pattern_result.get("setup_duration")
        pivot = pattern_result.get("pivot")
        if dur and pivot:
            positives.append(f"{pattern_name} over {dur} sessions, pivot at {pivot:.2f}")
    if metrics.get("range_ratio_5_20") is not None and metrics["range_ratio_5_20"] < 0.85:
        positives.append(f"5-day range contracted to {metrics['range_ratio_5_20']*100:.0f}% of the 20-day range")
    if metrics.get("vol_dry_up_ratio") is not None and metrics["vol_dry_up_ratio"] < 1.0:
        positives.append(f"5-day median volume fell to {metrics['vol_dry_up_ratio']*100:.0f}% of the 20-day median")
    if metrics.get("rs_3m") is not None and metrics["rs_3m"] > 0:
        positives.append(f"3-month relative strength vs benchmark is positive ({metrics['rs_3m']:.1f}%)")
    if metrics.get("ma_alignment") == "BULLISH_ALIGNED":
        positives.append("Moving averages are in bullish alignment (shorter above longer)")

    if not positives:
        positives.append("No strong positive confirmations beyond the base pattern quality itself")

    return positives[:3], risks[:2]


def analyze_symbol_explosive(symbol: str, name: str, exchange: str, scanner_sources: List[str],
                              sector: str, df: pd.DataFrame, nifty_1m: Optional[float],
                              nifty_3m: Optional[float], market_regime, config: ExplosiveConfig,
                              analysis_date: date, run_calibration: bool = True,
                              is_smallcap: bool = False,
                              min_traded_value_cr: Optional[float] = None) -> ExplosiveCandidate:
    """Full pipeline for ONE symbol: data quality -> patterns -> score ->
    calibration -> trigger/invalidation -> explanation -> final candidate.
    `df` must already be the symbol's OHLCV history through the analysis
    day ONLY (caller's responsibility - this function itself never fetches
    or knows about any day beyond what's in `df`)."""

    dq = data_quality_check(df, config, analysis_date)
    if dq["status"] == "INSUFFICIENT_DATA":
        return _rejected_candidate(symbol, name, exchange, scanner_sources, sector,
                                    df["Close"].iloc[-1] if len(df) else None, dq,
                                    f"Only {dq['usable_sessions']} usable sessions "
                                    f"(need >= {config.min_history_acceptable}) - INSUFFICIENT_DATA")

    close, high, low, volume = df["Close"], df["High"], df["Low"], df["Volume"]
    open_ = df["Open"] if "Open" in df.columns else close.shift(1).fillna(close.iloc[0])
    last_close = float(close.iloc[-1])

    traded_value_cr = float((close * volume).rolling(20).median().iloc[-1] / 1e7) if len(close) >= 20 else None
    zero_vol_pct = float((volume == 0).mean() * 100)
    liquidity_ok = (
        (min_traded_value_cr is None or (traded_value_cr is not None and traded_value_cr >= min_traded_value_cr))
        and last_close >= config.min_price
        and zero_vol_pct <= config.max_zero_volume_pct
    )
    if not liquidity_ok:
        return _rejected_candidate(symbol, name, exchange, scanner_sources, sector, last_close, dq,
                                    f"Fails liquidity filter: traded value Rs{traded_value_cr}Cr, "
                                    f"zero-volume days {zero_vol_pct:.1f}%, price {last_close}")

    atr_series = atr_fn(df, 14)
    atr_val = float(atr_series.iloc[-1]) if pd.notna(atr_series.iloc[-1]) else None

    pattern_bundle = detect_all_patterns(high, low, close, volume, atr_series, config)
    primary = pattern_bundle["primary_result"]
    primary_name = pattern_bundle["primary_pattern"]

    if not primary["detected"]:
        return _rejected_candidate(symbol, name, exchange, scanner_sources, sector, last_close, dq,
                                    f"No qualifying pattern detected: {primary['rejection_reason']}")

    ema20 = ema(close, 20)
    pct_above_ema20 = safe_div(last_close - ema20.iloc[-1], ema20.iloc[-1]) * 100 if ema20.iloc[-1] else None
    bb_pctile = bollinger_width_percentile(close)
    range_ratio = range_compression_ratio(high, low)
    dry_up = volume_dry_up_ratio(volume)
    ud_ratio = up_down_volume_ratio(close, volume)
    obv = obv_slope(close, volume)
    cq = candle_quality(open_.iloc[-1], high.iloc[-1], low.iloc[-1], last_close)
    avg_vol_20 = volume.rolling(20).mean().iloc[-1]
    rel_vol = safe_div(volume.iloc[-1], avg_vol_20)

    stock_1m = safe_div(last_close, close.iloc[-22]) * 100 - 100 if len(close) > 22 else None
    stock_3m = safe_div(last_close, close.iloc[-63]) * 100 - 100 if len(close) > 63 else None
    rs_1m = (stock_1m - nifty_1m) if (stock_1m is not None and nifty_1m is not None) else None
    rs_3m = (stock_3m - nifty_3m) if (stock_3m is not None and nifty_3m is not None) else None
    ema20_slope = series_slope_pct_per_day(ema20, window=10)
    sma50 = sma(close, 50)
    alignment = ma_alignment(last_close, ema(close, 10).iloc[-1], ema20.iloc[-1], sma50.iloc[-1],
                              sma(close, 100).iloc[-1] if len(close) >= 100 else None,
                              sma(close, 200).iloc[-1] if len(close) >= 200 else None)

    pivot = primary["pivot"]
    dist_to_pivot = safe_div(pivot - last_close, pivot) * 100 if pivot else None
    breakout_trigger = pivot * (1 + config.breakout_trigger_buffer_pct / 100) if pivot else None
    setup_low = primary["setup_low"]
    invalidation = (setup_low if setup_low is not None else
                    (last_close - config.invalidation_atr_mult * atr_val if atr_val else None))
    risk_to_invalidation_pct = safe_div(last_close - invalidation, last_close) * 100 if invalidation else None

    swings = find_swings(high.values, low.values, order=config.swing_order)
    next_resistance = nearest_resistance_above(swings, last_close)
    upside_before_resistance_pct = (safe_div(next_resistance - last_close, last_close) * 100
                                     if next_resistance else None)

    if (risk_to_invalidation_pct is not None and risk_to_invalidation_pct > config.max_risk_to_invalidation_pct):
        return _rejected_candidate(symbol, name, exchange, scanner_sources, sector, last_close, dq,
                                    f"Risk to invalidation {risk_to_invalidation_pct:.1f}% exceeds max "
                                    f"{config.max_risk_to_invalidation_pct}%")

    score_result = compute_explosive_score(
        pattern_quality=primary["quality"], is_bullish_pattern=True,
        compression_metrics=dict(bb_width_percentile=bb_pctile, range_ratio_5_20=range_ratio),
        trend_rs_metrics=dict(ma_alignment=alignment, rs_1m=rs_1m, rs_3m=rs_3m, ema20_slope=ema20_slope),
        volume_metrics=dict(vol_dry_up_ratio=dry_up, breakout_vol_ratio=rel_vol,
                             obv_slope=obv, up_down_vol_ratio=ud_ratio),
        pivot_candle_metrics=dict(distance_to_pivot_pct=dist_to_pivot, clv=cq.get("clv"),
                                   upper_wick_pct=cq.get("upper_wick_pct")),
        market_regime_score=market_regime.score_component, liquidity_ok=True,
        data_quality_pct=dq["completeness_pct"], pct_above_ema20=pct_above_ema20, config=config,
    )

    if score_result.hard_rejected or score_result.band == BAND_EXCLUDE:
        return _rejected_candidate(symbol, name, exchange, scanner_sources, sector, last_close, dq,
                                    score_result.hard_reject_reason or
                                    f"Final score {score_result.final_score} below minimum band threshold")

    prob_label, hit_rate, sample_size, confidence = PROB_LABEL_NOT_AVAILABLE, None, 0, "LOW"
    if run_calibration and len(close) >= config.min_history_acceptable:
        samples = run_symbol_calibration(df.reset_index(drop=True), config, is_smallcap=is_smallcap)
        bucket = _bucket_for_score(score_result.final_score)
        calib = summarize_calibration(samples, bucket, primary_name, config)
        prob_label, hit_rate, sample_size = calib.probability_label, calib.hit_rate_pct, calib.sample_size
        if sample_size >= 40:
            confidence = "MODERATE" if calib.hit_rate_pct and calib.hit_rate_pct < 60 else "MODERATE_HIGH"
        elif sample_size >= config.min_calibration_sample_size:
            confidence = "LOW_MODERATE"
        else:
            confidence = "LOW"

    positives, risks = build_explanation(primary, primary_name, score_result,
                                          dict(range_ratio_5_20=range_ratio, vol_dry_up_ratio=dry_up,
                                               rs_3m=rs_3m, ma_alignment=alignment))

    return ExplosiveCandidate(
        symbol=symbol, name=name, exchange=exchange, scanner_sources=scanner_sources, sector=sector,
        close=last_close, primary_pattern=primary_name, secondary_pattern=pattern_bundle["secondary_pattern"],
        setup_stage=score_result.setup_stage, setup_duration=primary.get("setup_duration"),
        pivot=pivot, distance_to_pivot_pct=dist_to_pivot, breakout_trigger=breakout_trigger,
        setup_low=setup_low, invalidation=invalidation, risk_to_invalidation_pct=risk_to_invalidation_pct,
        atr_pct=atr_pct_fn(atr_val, last_close), range_compression_5_20=range_ratio,
        bb_width_percentile=bb_pctile, volume_dry_up_ratio=dry_up, relative_volume=rel_vol,
        close_location_value=cq.get("clv"), rs_1m=rs_1m, rs_3m=rs_3m,
        market_regime=market_regime.classification, circuit_risk="UNKNOWN",
        pattern_score=score_result.pattern_score, compression_score=score_result.compression_score,
        trend_rs_score=score_result.trend_rs_score, volume_score=score_result.volume_score,
        market_score=score_result.market_score, penalty_total=score_result.penalty_total,
        final_score=score_result.final_score, probability_label=prob_label, hit_rate_pct=hit_rate,
        sample_size=sample_size, confidence=confidence, data_quality_pct=dq["completeness_pct"],
        data_quality_status=dq["status"], positive_reasons=positives, main_risks=risks,
        invalidation_reason=(f"Close below {invalidation:.2f} (setup low / {config.invalidation_atr_mult}x ATR)"
                              if invalidation else "N/A"),
        final_verdict=score_result.band, rejected=False, rejection_reason=None,
    )


def _bucket_for_score(score: float) -> str:
    if score >= 90: return "90-100"
    if score >= 82: return "82-89"
    if score >= 75: return "75-81"
    if score >= 68: return "68-74"
    return "below-68"


def _rejected_candidate(symbol, name, exchange, scanner_sources, sector, close, dq, reason) -> ExplosiveCandidate:
    return ExplosiveCandidate(
        symbol=symbol, name=name, exchange=exchange, scanner_sources=scanner_sources, sector=sector,
        close=close, primary_pattern="N/A", secondary_pattern=None, setup_stage="FAILED_SETUP",
        setup_duration=None, pivot=None, distance_to_pivot_pct=None, breakout_trigger=None,
        setup_low=None, invalidation=None, risk_to_invalidation_pct=None, atr_pct=None,
        range_compression_5_20=None, bb_width_percentile=None, volume_dry_up_ratio=None,
        relative_volume=None, close_location_value=None, rs_1m=None, rs_3m=None,
        market_regime="N/A", circuit_risk="UNKNOWN", pattern_score=0, compression_score=0,
        trend_rs_score=0, volume_score=0, market_score=0, penalty_total=0, final_score=0,
        probability_label=PROB_LABEL_NOT_AVAILABLE, hit_rate_pct=None, sample_size=0,
        confidence="N/A", data_quality_pct=dq["completeness_pct"], data_quality_status=dq["status"],
        positive_reasons=[], main_risks=[reason], invalidation_reason="N/A",
        final_verdict="DO NOT INCLUDE IN PRIMARY LIST", rejected=True, rejection_reason=reason,
    )


def merge_duplicate_sources(candidates_by_source: Dict[str, List[str]]) -> Dict[str, List[str]]:
    """Section 3: 'if a security appears in multiple scanners, merge it
    into one candidate and show all source scanners.' `candidates_by_source`
    is {scanner_name: [symbols]} - returns {symbol: [scanner_names]}."""
    merged: Dict[str, List[str]] = {}
    for source, symbols in candidates_by_source.items():
        for sym in symbols:
            merged.setdefault(sym, []).append(source)
    return merged
