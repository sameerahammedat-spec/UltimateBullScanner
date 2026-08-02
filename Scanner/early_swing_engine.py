"""Rule-based Early Swing Rally analysis engine.

The rules score is an explainable quality score, not a calibrated probability.
Historical probability is deliberately left unavailable until the separate
backtester has enough pooled, out-of-sample observations.
"""
from __future__ import annotations

from datetime import date
from typing import Dict, List, Optional, Tuple

import numpy as np
import pandas as pd

from early_swing_config import DEFAULT_EARLY_SWING_CONFIG, EarlySwingConfig
from early_swing_features import (
    adx, atr, candle_metrics, classify_market_regime, count_consecutive_expansion_candles,
    ema, linear_slope_pct, macd, nearest_resistances, normalise_ohlcv, pct_return,
    relative_strength, rsi, safe_div, sma,
)
from early_swing_models import EarlySwingCandidate, MarketRegime, UniverseRecord


def _value(series: pd.Series, index: int = -1) -> Optional[float]:
    if len(series) == 0:
        return None
    value = series.iloc[index]
    return None if pd.isna(value) else float(value)


def _data_quality(df: pd.DataFrame, config: EarlySwingConfig, analysis_date: Optional[date]) -> Tuple[float, str, bool]:
    completeness = float(df[["High", "Low", "Close", "Volume"]].notna().mean().mean() * 100) if len(df) else 0.0
    if len(df) < config.minimum_history_sessions:
        return completeness, "INSUFFICIENT_DATA", True
    latest = df.index[-1]
    latest_date = latest.date() if hasattr(latest, "date") else None
    stale = bool(analysis_date and latest_date and (analysis_date - latest_date).days > config.stale_calendar_days)
    return completeness, "STALE" if stale else "GOOD", stale


def _base_quality(df: pd.DataFrame, signal_idx: int, lookback: int, atr_series: pd.Series,
                  config: EarlySwingConfig) -> Optional[dict]:
    start = signal_idx - lookback
    if start < 0:
        return None
    base = df.iloc[start:signal_idx]
    if len(base) < lookback:
        return None
    pivot = float(base["High"].max())
    setup_low = float(base["Low"].min())
    depth = safe_div(pivot - setup_low, pivot, 1.0) * 100
    atr_at_signal = _value(atr_series, signal_idx)
    tolerance_pct = max(config.pivot_touch_tolerance_pct,
                        safe_div((atr_at_signal or 0.0) * 0.35, pivot, 0.0) * 100)
    touches = int(((pivot - base["High"]) / pivot * 100 <= tolerance_pct).sum())
    range5 = float((base["High"] - base["Low"]).iloc[-5:].mean())
    range20 = float((base["High"] - base["Low"]).iloc[-20:].mean()) if len(base) >= 20 else float((base["High"] - base["Low"]).mean())
    compression = safe_div(range5, range20, 1.0)
    last_half_low = float(base["Low"].iloc[len(base)//2:].min())
    first_half_low = float(base["Low"].iloc[:len(base)//2].min())
    rising_support = last_half_low >= first_half_low * 0.985
    if depth > config.maximum_base_depth_pct:
        return None
    if touches < config.minimum_pivot_touches and compression > 0.88:
        return None
    quality = 45.0
    quality += min(20.0, touches * 5.0)
    quality += max(0.0, min(20.0, (1.0 - compression) * 50.0))
    quality += 10.0 if rising_support else 0.0
    quality += max(0.0, 5.0 - depth / config.maximum_base_depth_pct * 5.0)
    return {
        "lookback": lookback, "pivot": pivot, "setup_low": setup_low, "depth_pct": depth,
        "touches": touches, "compression": compression, "rising_support": rising_support,
        "quality": min(100.0, quality),
    }


def _supporting_pattern(df: pd.DataFrame, signal_idx: int) -> str:
    if signal_idx >= 2:
        mother = df.iloc[signal_idx - 2]
        inside = df.iloc[signal_idx - 1]
        if inside["High"] <= mother["High"] and inside["Low"] >= mother["Low"]:
            return "Inside-Bar Breakout"
    if signal_idx >= 7:
        ranges = (df["High"] - df["Low"]).iloc[signal_idx - 7:signal_idx]
        if len(ranges) == 7 and ranges.iloc[-1] <= ranges.min() + 1e-12:
            return "NR7 Breakout"
    return "Consolidation Breakout"


def _detect_setup(df: pd.DataFrame, atr_series: pd.Series, config: EarlySwingConfig) -> Optional[dict]:
    candidates: List[dict] = []
    n = len(df)
    for age in range(1, config.rally_age_max_sessions + 1):
        signal_idx = n - age
        if signal_idx <= 31:
            continue
        prev_close = float(df["Close"].iloc[signal_idx - 1])
        signal = df.iloc[signal_idx]
        signal_ret = safe_div(float(signal["Close"] - prev_close), prev_close, 0.0) * 100
        prior_volume = float(df["Volume"].iloc[max(0, signal_idx - 20):signal_idx].median())
        volume_ratio = safe_div(float(signal["Volume"]), prior_volume, 0.0)
        cm = candle_metrics(float(signal["Open"]), float(signal["High"]), float(signal["Low"]), float(signal["Close"]))
        for lookback in config.breakout_lookbacks:
            base = _base_quality(df, signal_idx, lookback, atr_series, config)
            if not base:
                continue
            pivot = base["pivot"]
            breakout = float(signal["Close"]) >= pivot * (1 + config.breakout_buffer_pct / 100)
            intraday_breakout_hold = float(signal["High"]) >= pivot * (1 + config.breakout_buffer_pct / 100) and float(signal["Close"]) >= pivot
            if not (breakout or intraday_breakout_hold):
                continue
            if signal_ret < config.breakout_min_return_pct:
                continue
            if volume_ratio < config.breakout_min_volume_ratio:
                continue
            if cm["clv"] < config.minimum_close_location_value:
                continue
            followthrough_quality = 100.0
            if age == 2:
                current = df.iloc[-1]
                current_prior_volume = float(df["Volume"].iloc[-21:-1].median())
                current_vol_ratio = safe_div(float(current["Volume"]), current_prior_volume, 0.0)
                current_cm = candle_metrics(float(current["Open"]), float(current["High"]), float(current["Low"]), float(current["Close"]))
                holds_pivot = float(current["Close"]) >= pivot and float(current["Close"]) >= (float(signal["Open"]) + float(signal["Close"])) / 2
                if not holds_pivot or current_cm["clv"] < 0.38:
                    continue
                if current_vol_ratio < config.followthrough_min_volume_ratio:
                    followthrough_quality -= 25.0
                if float(current["Close"]) < float(signal["Close"]):
                    followthrough_quality -= 10.0
            quality = base["quality"] * 0.70 + min(100.0, volume_ratio / 2.0 * 100) * 0.20 + followthrough_quality * 0.10
            candidates.append({
                **base, "rally_age": age, "signal_idx": signal_idx,
                "signal_date": str(df.index[signal_idx].date() if hasattr(df.index[signal_idx], "date") else df.index[signal_idx]),
                "signal_return_pct": signal_ret, "breakout_volume_ratio": volume_ratio,
                "signal_low": float(signal["Low"]), "signal_high": float(signal["High"]),
                "signal_close": float(signal["Close"]), "signal_clv": cm["clv"],
                "signal_upper_wick_pct": cm["upper_wick_pct"],
                "setup_type": _supporting_pattern(df, signal_idx), "quality": min(100.0, quality),
            })
    return max(candidates, key=lambda c: c["quality"]) if candidates else None


def _empty_candidate(record: UniverseRecord, df: pd.DataFrame, reason: str, category: str,
                     data_quality_score: float = 0.0) -> EarlySwingCandidate:
    close = float(df["Close"].iloc[-1]) if len(df) else None
    latest = str(df.index[-1].date() if len(df) and hasattr(df.index[-1], "date") else "")
    return EarlySwingCandidate(
        symbol=record.symbol, ticker=record.ticker, company_name=record.company_name,
        exchange=record.exchange, bse_group=record.bse_group, sector=record.sector,
        source_universes=record.source_universes, setup_date="", current_price=close,
        rally_age=None, setup_type="N/A", pivot=None, setup_low=None, entry_low=None,
        entry_high=None, stop_loss=None, target1=None, target2=None, risk_per_share=None,
        stop_distance_pct=None, reward_risk=None, position_size=None, nearest_resistance=None,
        distance_to_resistance_pct=None, one_day_return_pct=None, two_day_return_pct=None,
        five_day_return_pct=None, ten_day_return_pct=None, breakout_volume_ratio=None,
        current_volume_ratio=None, rsi=None, adx=None, atr_pct=None, macd_histogram=None,
        relative_strength_2d=None, relative_strength_5d=None, relative_strength_10d=None,
        market_regime="N/A", extension_risk=0.0, false_breakout_risk=0.0,
        data_quality_score=data_quality_score, rules_score=0.0, confidence="N/A",
        delivery_status="NOT_AVAILABLE", suggested_status="Avoid", reasons=[],
        warning_flags=[reason], component_scores={}, penalties=[], rejected=True,
        rejection_category=category, rejection_reason=reason, usable_sessions=len(df),
        latest_data_date=latest,
    )


def analyze_early_swing(record: UniverseRecord, stock_df: pd.DataFrame, benchmark_df: pd.DataFrame,
                        config: EarlySwingConfig = DEFAULT_EARLY_SWING_CONFIG,
                        analysis_date: Optional[date] = None) -> EarlySwingCandidate:
    df = normalise_ohlcv(stock_df)
    benchmark = normalise_ohlcv(benchmark_df)
    analysis_date = analysis_date or date.today()
    completeness, quality_status, stale = _data_quality(df, config, analysis_date)
    data_quality_score = min(5.0, completeness / 100 * 5.0)
    if quality_status == "INSUFFICIENT_DATA":
        return _empty_candidate(record, df, f"Only {len(df)} usable sessions; need {config.minimum_history_sessions}", "INSUFFICIENT_DATA", data_quality_score)
    if stale:
        return _empty_candidate(record, df, "Latest price data is stale", "INSUFFICIENT_DATA", data_quality_score)

    close, volume = df["Close"], df["Volume"]
    last_close = float(close.iloc[-1])
    median_traded_value = float((close * volume).rolling(20).median().iloc[-1] / 1e7)
    zero_volume_pct = float((volume == 0).mean() * 100)
    if last_close < config.minimum_price or median_traded_value < config.minimum_median_traded_value_cr or zero_volume_pct > config.maximum_zero_volume_pct:
        return _empty_candidate(
            record, df,
            f"Liquidity failed: price={last_close:.2f}, median traded value={median_traded_value:.2f} Cr, zero-volume={zero_volume_pct:.1f}%",
            "LIQUIDITY", data_quality_score,
        )

    atr_s = atr(df, 14)
    setup = _detect_setup(df, atr_s, config)
    if setup is None:
        return _empty_candidate(record, df, "No valid one- or two-session early-rally initiation with preceding structure", "NO_SETUP", data_quality_score)

    ema10, ema20, sma50 = ema(close, 10), ema(close, 20), sma(close, 50)
    rsi_s, adx_s = rsi(close), adx(df)
    _, _, macd_hist = macd(close)
    atr_value = _value(atr_s) or 0.0
    rsi_value = _value(rsi_s)
    adx_value = _value(adx_s)
    macd_value = _value(macd_hist)
    current_cm = candle_metrics(float(df["Open"].iloc[-1]), float(df["High"].iloc[-1]),
                                float(df["Low"].iloc[-1]), last_close)
    current_volume_median = float(volume.iloc[-21:-1].median()) if len(volume) >= 21 else float(volume.median())
    current_volume_ratio = safe_div(float(volume.iloc[-1]), current_volume_median, 0.0)

    returns = {n: pct_return(close, n) for n in (1, 2, 3, 5, 10)}
    above_ema20_pct = safe_div(last_close - float(ema20.iloc[-1]), float(ema20.iloc[-1]), 0.0) * 100
    dist_above_pivot_pct = safe_div(last_close - setup["pivot"], setup["pivot"], 0.0) * 100
    consecutive_expansion = count_consecutive_expansion_candles(df, atr_s)

    regime = classify_market_regime(benchmark)
    rs2 = relative_strength(close, benchmark["Close"], 2) if len(benchmark) else None
    rs5 = relative_strength(close, benchmark["Close"], 5) if len(benchmark) else None
    rs10 = relative_strength(close, benchmark["Close"], 10) if len(benchmark) else None

    # Extension risk (0-100).
    extension = 0.0
    if above_ema20_pct > config.maximum_above_ema20_pct:
        extension += min(35.0, (above_ema20_pct - config.maximum_above_ema20_pct) * 4.0 + 15.0)
    if (returns[3] or 0) > config.maximum_three_day_return_pct:
        extension += 20.0
    if (returns[5] or 0) > config.maximum_five_day_return_pct:
        extension += 20.0
    if (returns[10] or 0) > config.maximum_ten_day_return_pct:
        extension += 15.0
    if rsi_value is not None and rsi_value > config.hard_maximum_rsi:
        extension += 20.0
    if consecutive_expansion > config.maximum_consecutive_expansion_candles:
        extension += 15.0
    if atr_value and dist_above_pivot_pct > safe_div(2.5 * atr_value, setup["pivot"], 0.0) * 100:
        extension += 10.0
    extension = min(100.0, extension)

    # False-breakout risk (0-100).
    false_risk = 0.0
    if last_close < setup["pivot"]:
        false_risk += 55.0
    if current_cm["upper_wick_pct"] > config.maximum_upper_wick_pct:
        false_risk += 22.0
    if setup["breakout_volume_ratio"] < config.breakout_min_volume_ratio:
        false_risk += 25.0
    if setup["rally_age"] == 2 and current_volume_ratio < config.followthrough_min_volume_ratio:
        false_risk += 18.0
    if current_cm["clv"] < 0.35:
        false_risk += 18.0
    if regime.classification == "BEARISH":
        false_risk += 15.0
    false_risk = min(100.0, false_risk)

    resistances = nearest_resistances(df["High"].iloc[:-1], last_close)
    nearest_resistance = resistances[0] if resistances else None
    distance_resistance = safe_div(nearest_resistance - last_close, last_close, None) * 100 if nearest_resistance else None

    pivot = float(setup["pivot"])
    entry_low = max(last_close, pivot * (1 + config.entry_buffer_pct / 100))
    entry_high = pivot * (1 + config.maximum_chase_above_pivot_pct / 100)
    stop_candidates = [
        float(setup["setup_low"]),
        pivot - config.atr_stop_multiple * atr_value,
        float(setup["signal_low"]) - config.stop_buffer_atr * atr_value,
    ]
    valid_stops = [s for s in stop_candidates if np.isfinite(s) and s < entry_low]
    stop_loss = max(valid_stops) if valid_stops else entry_low - config.atr_stop_multiple * atr_value
    risk_per_share = max(0.0, entry_low - stop_loss)
    stop_distance_pct = safe_div(risk_per_share, entry_low, 0.0) * 100
    available_upside = (nearest_resistance - entry_low) if nearest_resistance else config.target2_r_multiple * risk_per_share
    reward_risk = safe_div(available_upside, risk_per_share, 0.0)
    target1 = entry_low + config.target1_r_multiple * risk_per_share
    target2 = entry_low + config.target2_r_multiple * risk_per_share
    if nearest_resistance:
        target1 = min(target1, nearest_resistance * 0.995)
        target2 = min(target2, nearest_resistance * 0.995)
    account_risk = config.account_size * config.account_risk_pct / 100
    position_size = int(account_risk // risk_per_share) if risk_per_share > 0 else None

    if extension >= config.maximum_extension_risk:
        return _empty_candidate(record, df, f"Extension risk {extension:.0f}/100 exceeds limit", "EXTENDED", data_quality_score)
    if false_risk >= config.maximum_false_breakout_risk:
        return _empty_candidate(record, df, f"False-breakout risk {false_risk:.0f}/100 exceeds limit", "FALSE_BREAKOUT", data_quality_score)
    if stop_distance_pct > config.maximum_stop_distance_pct:
        return _empty_candidate(record, df, f"Required stop distance {stop_distance_pct:.1f}% is too wide", "POOR_RISK_REWARD", data_quality_score)
    if reward_risk < config.minimum_reward_risk:
        return _empty_candidate(record, df, f"Available reward-to-risk {reward_risk:.2f} is below {config.minimum_reward_risk:.2f}", "POOR_RISK_REWARD", data_quality_score)
    if distance_resistance is not None and distance_resistance < config.minimum_distance_to_resistance_pct:
        return _empty_candidate(record, df, f"Nearest resistance is only {distance_resistance:.1f}% away", "POOR_RISK_REWARD", data_quality_score)

    # Explainable component score.
    weights = config.score_weights
    components: Dict[str, float] = {}
    components["setup"] = min(weights["setup"], setup["quality"] / 100 * weights["setup"])

    volume_strength = min(1.0, setup["breakout_volume_ratio"] / 2.0)
    followthrough_strength = min(1.0, current_volume_ratio / 1.3)
    components["volume"] = weights["volume"] * (0.70 * volume_strength + 0.30 * followthrough_strength)

    trend_points = 0.0
    trend_points += 0.30 if last_close > float(ema10.iloc[-1]) > float(ema20.iloc[-1]) else 0.0
    trend_points += 0.20 if last_close > float(sma50.iloc[-1]) else 0.0
    trend_points += 0.20 if (linear_slope_pct(ema20, 10) or 0.0) > 0 else 0.0
    if rsi_value is not None and config.preferred_rsi_range[0] <= rsi_value <= config.preferred_rsi_range[1]:
        trend_points += 0.15
    if adx_value is not None and adx_value >= config.minimum_adx:
        trend_points += 0.10
    if macd_value is not None and macd_value > 0:
        trend_points += 0.05
    components["trend_momentum"] = weights["trend_momentum"] * min(1.0, trend_points)

    rs_values = [x for x in (rs2, rs5, rs10) if x is not None]
    positive_rs = sum(1 for x in rs_values if x > 0) / len(rs_values) if rs_values else 0.0
    market_fraction = regime.score / 12.0
    components["relative_strength_market"] = weights["relative_strength_market"] * (0.65 * positive_rs + 0.35 * market_fraction)

    resistance_fraction = 1.0 if distance_resistance is None else min(1.0, distance_resistance / 10.0)
    rr_fraction = min(1.0, reward_risk / 3.0)
    components["remaining_upside"] = weights["remaining_upside"] * (0.55 * resistance_fraction + 0.45 * rr_fraction)

    stop_fraction = max(0.0, 1.0 - stop_distance_pct / config.maximum_stop_distance_pct)
    chase_fraction = max(0.0, 1.0 - max(0.0, dist_above_pivot_pct) / config.maximum_chase_above_pivot_pct)
    components["trade_structure"] = weights["trade_structure"] * (0.55 * stop_fraction + 0.45 * chase_fraction)
    components["data_liquidity"] = data_quality_score

    # Delivery is unavailable with Yahoo-only data. Exclude it from the denominator
    # instead of awarding or deducting fabricated points.
    available_max = sum(v for k, v in weights.items() if k != "delivery")
    raw_available = sum(components.values())
    normalised_score = raw_available / available_max * 100.0

    penalties: List[str] = []
    penalty_total = 0.0
    if extension >= 45:
        p = 6.0 if extension < 60 else 12.0
        penalty_total += p
        penalties.append(f"Extension risk penalty -{p:.0f}")
    if false_risk >= 35:
        p = 5.0 if false_risk < 50 else 10.0
        penalty_total += p
        penalties.append(f"False-breakout risk penalty -{p:.0f}")
    if regime.classification in {"BEARISH", "HIGH_VOLATILITY"}:
        penalty_total += 5.0
        penalties.append("Unfavourable market-regime penalty -5")
    rules_score = max(0.0, min(100.0, normalised_score - penalty_total))

    reasons = [
        f"{setup['setup_type']} with rally age {setup['rally_age']} session(s)",
        f"Breakout volume {setup['breakout_volume_ratio']:.2f}x recent median",
        f"Base depth {setup['depth_pct']:.1f}% with {setup['touches']} pivot touch(es)",
    ]
    if rs5 is not None and rs5 > 0:
        reasons.append(f"5-session relative strength is +{rs5:.1f}% versus benchmark")
    if reward_risk >= 2:
        reasons.append(f"Available reward-to-risk is {reward_risk:.2f}:1")

    warnings: List[str] = []
    if current_volume_ratio < 0.8:
        warnings.append("Follow-through volume is below its recent median")
    if current_cm["upper_wick_pct"] > 30:
        warnings.append(f"Upper wick is {current_cm['upper_wick_pct']:.0f}% of candle range")
    if dist_above_pivot_pct > config.maximum_chase_above_pivot_pct:
        warnings.append("Price is above the maximum chase level")
    if regime.classification in {"BEARISH", "HIGH_VOLATILITY"}:
        warnings.append(f"Market regime is {regime.classification}")

    above_entry = last_close > entry_high
    if rules_score >= config.exceptional_score and not above_entry:
        status, confidence = "Enter", "HIGH"
    elif rules_score >= config.high_quality_score:
        status, confidence = ("Wait for Pullback" if above_entry else "Enter"), "MODERATE_HIGH"
    elif rules_score >= config.watchlist_score:
        status, confidence = "Watch", "MODERATE"
    else:
        status, confidence = "Avoid", "LOW"

    latest = str(df.index[-1].date() if hasattr(df.index[-1], "date") else df.index[-1])
    return EarlySwingCandidate(
        symbol=record.symbol, ticker=record.ticker, company_name=record.company_name,
        exchange=record.exchange, bse_group=record.bse_group, sector=record.sector,
        source_universes=record.source_universes, setup_date=setup["signal_date"],
        current_price=last_close, rally_age=setup["rally_age"], setup_type=setup["setup_type"],
        pivot=pivot, setup_low=float(setup["setup_low"]), entry_low=entry_low, entry_high=entry_high,
        stop_loss=stop_loss, target1=target1, target2=target2, risk_per_share=risk_per_share,
        stop_distance_pct=stop_distance_pct, reward_risk=reward_risk, position_size=position_size,
        nearest_resistance=nearest_resistance, distance_to_resistance_pct=distance_resistance,
        one_day_return_pct=returns[1], two_day_return_pct=returns[2], five_day_return_pct=returns[5],
        ten_day_return_pct=returns[10], breakout_volume_ratio=setup["breakout_volume_ratio"],
        current_volume_ratio=current_volume_ratio, rsi=rsi_value, adx=adx_value,
        atr_pct=safe_div(atr_value, last_close, 0.0) * 100, macd_histogram=macd_value,
        relative_strength_2d=rs2, relative_strength_5d=rs5, relative_strength_10d=rs10,
        market_regime=regime.classification, extension_risk=extension,
        false_breakout_risk=false_risk, data_quality_score=data_quality_score,
        rules_score=rules_score, confidence=confidence, delivery_status="NOT_AVAILABLE",
        suggested_status=status, reasons=reasons[:5], warning_flags=warnings[:5],
        component_scores={k: round(v, 2) for k, v in components.items()}, penalties=penalties,
        rejected=(status == "Avoid"), rejection_category="LOW_SCORE" if status == "Avoid" else "",
        rejection_reason=f"Rules score {rules_score:.1f} below actionable threshold" if status == "Avoid" else "",
        usable_sessions=len(df), latest_data_date=latest,
    )
