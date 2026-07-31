"""
Numerical pattern detectors for the Next-Day Explosive Move Engine.

WHAT'S IMPLEMENTED HERE (a deliberately scoped subset, not all patterns
from the original spec - see the module docstring in explosive_engine.py
for why): Volatility Contraction Pattern (VCP), Tight/Flat Base, Ascending
Triangle, Bull Flag. Plus the cheap supporting signals (inside bar, NR7,
range percentile) that don't warrant their own full detector.

DEFERRED (not built, not faked): Shakeout & Reclaim, Breakout & Retest,
Rounding Base/Cup. These need materially more geometric logic to do well;
better to ship 4 solid detectors than 7 shaky ones. See explosive_engine.py
for the full honesty note.

Every detector takes OHLCV data already sliced to end at the analysis day
(no look-ahead - the caller's job, not this file's) and returns a dict:
    detected, quality (0-100), setup_start_index, setup_duration,
    pivot, setup_low, metrics (dict), rejection_reason (str or None)
"""

from typing import Optional
import numpy as np
import pandas as pd

from explosive_swings import find_swings, resistance_quality, support_quality
from explosive_indicators import safe_div, range_compression_ratio


def _empty_result(reason: str) -> dict:
    return dict(detected=False, quality=0.0, setup_start_index=None, setup_duration=None,
                pivot=None, setup_low=None, metrics={}, rejection_reason=reason)


def detect_vcp(high: pd.Series, low: pd.Series, close: pd.Series, volume: pd.Series,
               atr_series: pd.Series, lookback: int = 40, swing_order: int = 3,
               min_swings: int = 2) -> dict:
    """Volatility Contraction Pattern: progressively smaller swings, ATR
    contracting, volume contracting during the base, price staying near
    resistance, swing lows holding or rising. Requires trend + location +
    volume confirmation - a merely-quiet stock is not automatically a VCP."""
    n = len(close)
    if n < lookback + swing_order * 2:
        return _empty_result("insufficient history for VCP lookback window")

    h, l, c, v = high.iloc[-lookback:], low.iloc[-lookback:], close.iloc[-lookback:], volume.iloc[-lookback:]
    swings = find_swings(h.values, l.values, order=swing_order)
    swing_highs = [s for s in swings if s.is_high]
    swing_lows = [s for s in swings if not s.is_high]

    if len(swing_highs) < min_swings or len(swing_lows) < min_swings:
        return _empty_result(f"fewer than {min_swings} confirmed swing highs/lows in the lookback window")

    all_swings_sorted = sorted(swings, key=lambda s: s.index)
    amplitudes = [abs(all_swings_sorted[i].price - all_swings_sorted[i - 1].price)
                  for i in range(1, len(all_swings_sorted))]
    if len(amplitudes) < 4:
        return _empty_result("not enough successive swing legs to assess a genuine contraction trend "
                              "(need at least 4 legs, not just a first-vs-last comparison)")

    # Real contraction = amplitudes trending DOWN across the whole window, not
    # just the last leg happening to be smaller than the first by chance.
    amp_x = np.arange(len(amplitudes), dtype=float)
    amp_y = np.array(amplitudes, dtype=float)
    if np.std(amp_y) < 1e-9:
        return _empty_result("swing amplitudes show no meaningful variation")
    slope, intercept = np.polyfit(amp_x, amp_y, 1)
    y_hat = slope * amp_x + intercept
    ss_res = np.sum((amp_y - y_hat) ** 2)
    ss_tot = np.sum((amp_y - amp_y.mean()) ** 2)
    amp_r2 = 1 - ss_res / ss_tot if ss_tot > 1e-9 else 0.0
    mean_amp = amp_y.mean() or 1.0
    normalized_slope = slope / mean_amp
    contracting = bool(normalized_slope < -0.03 and amp_r2 > 0.25)
    contraction_ratio = safe_div(amplitudes[-1], amplitudes[0])

    # ATR contraction AND range compression must BOTH agree (correlated
    # measures - requiring only one lets noise slip through on whichever
    # measure happened to dip by chance).
    atr_recent = atr_series.iloc[-5:].mean()
    atr_prior = atr_series.iloc[-lookback:-5].mean() if lookback > 5 else None
    atr_contraction_pct = safe_div(atr_prior - atr_recent, atr_prior) if (atr_recent is not None and atr_prior) else None
    atr_contracting = atr_contraction_pct is not None and atr_contraction_pct > 0.15

    range_ratio_5_20 = range_compression_ratio(h, l, recent=5, baseline=20)
    range_contracting = range_ratio_5_20 is not None and range_ratio_5_20 < 0.85

    volatility_contracting = atr_contracting and range_contracting

    vol_recent = v.iloc[-5:].mean()
    vol_prior = v.iloc[-lookback:-5].mean() if lookback > 5 else None
    vol_contraction_pct = safe_div(vol_prior - vol_recent, vol_prior) if (vol_recent is not None and vol_prior) else None
    vol_contracting = vol_contraction_pct is not None and vol_contraction_pct > 0.15

    swing_lows_sorted = sorted(swing_lows, key=lambda s: s.index)
    lows_rising = swing_lows_sorted[-1].price >= swing_lows_sorted[0].price * 0.98 if len(swing_lows_sorted) >= 2 else False

    resistance = resistance_quality(swings, current_index=len(h) - 1, atr_value=atr_series.iloc[-1])
    near_resistance = (resistance is not None and resistance.touches >= 2 and c.iloc[-1] >= resistance.price * 0.97)

    # ALL FIVE must hold - a strict conjunction on purpose. VCP is a specific,
    # fairly rare setup; a majority vote across loosely-correlated checks is
    # exactly what let random noise through in testing.
    all_confirmed = contracting and volatility_contracting and vol_contracting and lows_rising and near_resistance
    checks_passed = sum([contracting, volatility_contracting, vol_contracting, lows_rising, near_resistance])

    if not all_confirmed:
        missing = [name for name, ok in [
            ("swing-amplitude contraction", contracting),
            ("ATR+range volatility contraction", volatility_contracting),
            ("volume contraction >15%", vol_contracting),
            ("stable/rising swing lows", lows_rising),
            ("price near a 2+-touch resistance", near_resistance),
        ] if not ok]
        return _empty_result(f"VCP requires ALL 5 confirmations; missing: {', '.join(missing)} "
                              f"({checks_passed}/5 met)")

    quality = min(100.0, 55 + amp_r2 * 20 + (vol_contraction_pct or 0) * 30)
    setup_start = sorted(swing_highs, key=lambda s: s.index)[0].index if swing_highs else 0
    return dict(
        detected=True, quality=round(quality, 1),
        setup_start_index=setup_start, setup_duration=len(h) - setup_start,
        pivot=(resistance.price if resistance else float(h.max())),
        setup_low=float(l.iloc[setup_start:].min()),
        metrics=dict(contraction_ratio=contraction_ratio, amp_r2=amp_r2,
                     atr_contraction_pct=atr_contraction_pct, range_ratio_5_20=range_ratio_5_20,
                     vol_contraction_pct=vol_contraction_pct, lows_rising=lows_rising,
                     checks_passed=checks_passed),
        rejection_reason=None,
    )


def detect_tight_base(high: pd.Series, low: pd.Series, close: pd.Series, volume: pd.Series,
                       min_days: int = 5, max_days: int = 25, max_depth_pct: float = 15.0) -> dict:
    """Tight/Flat Base: 5-25 sessions of consolidation, limited peak-to-
    trough depth, repeated resistance near a common price, controlled
    pullbacks, stable/rising lows, reduced volume late in the base, close
    near the upper part of the range."""
    n = len(close)
    # A genuinely "tight" base is meaningfully tighter than the hard ceiling,
    # not merely under it - the ceiling alone let short trending-noise windows
    # slip through by chance (checked across many window sizes, take-the-best
    # is a multiple-comparisons trap without this margin).
    tight_depth_threshold = max_depth_pct * 0.7
    best = None
    for window in range(min_days, min(max_days, n) + 1):
        h, l, c, v = high.iloc[-window:], low.iloc[-window:], close.iloc[-window:], volume.iloc[-window:]
        peak, trough = h.max(), l.min()
        depth_pct = safe_div(peak - trough, peak) * 100 if peak else None
        if depth_pct is None or depth_pct > tight_depth_threshold:
            continue

        # Net drift over the window must ALSO be small - this is what actually
        # separates a sideways base from a short window that happened to catch
        # a low-depth slice of an ongoing trend (peak-trough can be small even
        # mid-trend over a short enough window).
        net_drift_pct = safe_div(abs(c.iloc[-1] - c.iloc[0]), c.iloc[0]) * 100 if c.iloc[0] else None
        is_sideways = net_drift_pct is not None and net_drift_pct <= tight_depth_threshold * 0.8
        if not is_sideways:
            continue

        half = window // 2
        if half < 2:
            continue
        first_half_low, second_half_low = l.iloc[:half].min(), l.iloc[half:].min()
        lows_stable = bool(second_half_low >= first_half_low * 0.97)
        vol_first, vol_second = v.iloc[:half].mean(), v.iloc[half:].mean()
        vol_reducing = bool(vol_second < vol_first * 0.9) if vol_first else False   # >=10% real reduction
        rng = peak - trough
        clv = safe_div(c.iloc[-1] - trough, rng) if rng else None
        upper_range = bool(clv is not None and clv >= 0.6)

        # ALL THREE confirmations required - "2 of 3" was the other half of
        # what let trending noise through in testing.
        if not (lows_stable and vol_reducing and upper_range):
            continue

        depth_margin = tight_depth_threshold - depth_pct  # how comfortably it beat the threshold
        score = depth_margin
        if best is None or score > best[0]:
            best = (score, window, depth_pct, lows_stable, vol_reducing, clv, peak, trough, net_drift_pct)

    if best is None:
        return _empty_result(f"no {min_days}-{max_days} day window found that is genuinely sideways "
                              f"(depth <= {tight_depth_threshold:.1f}%, low net drift) AND has ALL 3 "
                              f"confirmations (stable lows, real volume reduction, upper-range close)")

    score, window, depth_pct, lows_stable, vol_reducing, clv, peak, trough, net_drift_pct = best
    quality = min(100.0, 55 + score * 3 + (clv or 0) * 20)
    return dict(
        detected=True, quality=round(quality, 1),
        setup_start_index=n - window, setup_duration=window,
        pivot=float(peak), setup_low=float(trough),
        metrics=dict(depth_pct=depth_pct, net_drift_pct=net_drift_pct, lows_stable=lows_stable,
                     vol_reducing=vol_reducing, clv=clv),
        rejection_reason=None,
    )


def detect_ascending_triangle(high: pd.Series, low: pd.Series, close: pd.Series,
                               atr_value: Optional[float], lookback: int = 60,
                               swing_order: int = 3) -> dict:
    """At least 2 highs near the same resistance (ATR-tolerance, never
    exact equality), rising swing lows, declining downside volatility,
    price near the breakout pivot."""
    n = len(close)
    if n < lookback:
        lookback = n
    if lookback < swing_order * 4:
        return _empty_result("insufficient history for ascending-triangle lookback")

    h, l, c = high.iloc[-lookback:], low.iloc[-lookback:], close.iloc[-lookback:]
    swings = find_swings(h.values, l.values, order=swing_order)
    resistance = resistance_quality(swings, current_index=lookback - 1, atr_value=atr_value)
    support = support_quality(swings, current_index=lookback - 1, atr_value=atr_value)

    if resistance is None or resistance.touches < 2 or resistance.quality < 45:
        return _empty_result("fewer than 2 genuine touches at a common resistance level "
                              "(or the level is too loosely defined)")
    if support is None or support.touches < 2:
        return _empty_result("fewer than 2 genuine touches confirming a rising support level "
                              "(a single incidental swing low is not a support line)")

    swing_lows_sorted = sorted([s for s in swings if not s.is_high], key=lambda s: s.index)
    lows_rising = len(swing_lows_sorted) >= 2 and swing_lows_sorted[-1].price > swing_lows_sorted[0].price

    if not lows_rising:
        return _empty_result("swing lows are not rising")

    near_pivot = c.iloc[-1] >= resistance.price * 0.97

    if not near_pivot:
        return _empty_result(f"price {c.iloc[-1]:.2f} not yet near pivot {resistance.price:.2f}")

    quality = min(100.0, resistance.quality * 0.5 + support.quality * 0.3 + 20)
    setup_start = min(s.index for s in swings) if swings else 0
    return dict(
        detected=True, quality=round(quality, 1),
        setup_start_index=setup_start, setup_duration=lookback - setup_start,
        pivot=resistance.price, setup_low=float(l.iloc[setup_start:].min()),
        metrics=dict(resistance_touches=resistance.touches, resistance_quality=resistance.quality,
                     support_quality=support.quality, lows_rising=lows_rising),
        rejection_reason=None,
    )


def detect_bull_flag(high: pd.Series, low: pd.Series, close: pd.Series, volume: pd.Series,
                      max_retracement_pct: float = 50.0, max_flag_range_pct: float = 8.0) -> dict:
    """Genuine prior impulse (the 'pole'), shallow orderly consolidation
    (the 'flag') retracing under max_retracement_pct of the pole, declining
    volume during the flag, price setting up to break the flag's upper
    boundary."""
    n = len(close)
    if n < 20:
        return _empty_result("insufficient history (need >= 20 sessions)")

    pole_window = min(30, n // 2)
    pole = close.iloc[-(pole_window * 2):-pole_window]
    flag = close.iloc[-pole_window:]
    flag_high, flag_low = high.iloc[-pole_window:], low.iloc[-pole_window:]

    if len(pole) < 5:
        return _empty_result("pole segment too short")

    pole_move = pole.iloc[-1] - pole.iloc[0]
    if pole_move <= 0:
        return _empty_result("no genuine prior up-move (pole) detected")

    pole_move_pct = safe_div(pole_move, pole.iloc[0]) * 100 if pole.iloc[0] else None
    if pole_move_pct is None or pole_move_pct < 8.0:
        return _empty_result(f"prior move too small to count as an impulse pole "
                              f"({pole_move_pct}% over {len(pole)} sessions, need >= 8%)")

    flag_retracement = (pole.iloc[-1] - flag.min()) / pole_move * 100 if pole_move else None
    if flag_retracement is None or flag_retracement > max_retracement_pct:
        return _empty_result(f"flag retraced {flag_retracement}% of the pole "
                              f"(max allowed {max_retracement_pct}%)")

    flag_range_pct = safe_div(flag_high.max() - flag_low.min(), flag.mean()) * 100 if flag.mean() else None
    if flag_range_pct is None or flag_range_pct > max_flag_range_pct:
        return _empty_result(f"flag range too wide ({flag_range_pct}%, max {max_flag_range_pct}%) "
                              f"to count as an orderly consolidation")

    vol_pole = volume.iloc[-(pole_window * 2):-pole_window].mean()
    vol_flag = volume.iloc[-pole_window:].mean()
    vol_declining = vol_flag < vol_pole if vol_pole else False

    quality = min(100.0, 40 + (pole_move_pct or 0) * 1.5 + (10 if vol_declining else 0)
                  + max(0, (max_flag_range_pct - (flag_range_pct or max_flag_range_pct)) * 2))
    setup_start = n - pole_window
    return dict(
        detected=True, quality=round(quality, 1),
        setup_start_index=setup_start, setup_duration=pole_window,
        pivot=float(flag_high.max()), setup_low=float(flag_low.min()),
        metrics=dict(pole_move_pct=pole_move_pct, flag_retracement_pct=flag_retracement,
                     flag_range_pct=flag_range_pct, vol_declining=vol_declining),
        rejection_reason=None,
    )


def detect_all_patterns(high: pd.Series, low: pd.Series, close: pd.Series, volume: pd.Series,
                         atr_series: pd.Series, config) -> dict:
    """Runs all 4 detectors and returns the best-quality DETECTED pattern
    as primary, second-best (if also detected) as secondary. If nothing is
    detected, primary is 'No Clear Pattern' with all rejection reasons kept
    for the explanation engine."""
    atr_value = atr_series.iloc[-1] if len(atr_series) else None
    results = {
        "Volatility Contraction Pattern": detect_vcp(
            high, low, close, volume, atr_series,
            lookback=config.vcp_lookback, swing_order=config.swing_order,
            min_swings=config.vcp_min_swings),
        "Tight Base": detect_tight_base(
            high, low, close, volume,
            min_days=config.tight_base_min_days, max_days=config.tight_base_max_days,
            max_depth_pct=config.tight_base_max_depth_pct),
        "Ascending Triangle": detect_ascending_triangle(
            high, low, close, atr_value,
            lookback=config.ascending_triangle_lookback, swing_order=config.swing_order),
        "Bull Flag": detect_bull_flag(
            high, low, close, volume,
            max_retracement_pct=config.bull_flag_max_retracement_pct,
            max_flag_range_pct=config.bull_flag_max_flag_range_pct),
    }

    detected = {name: r for name, r in results.items() if r["detected"]}
    if not detected:
        return dict(primary_pattern="No Clear Pattern", primary_result=_empty_result("no pattern detected"),
                    secondary_pattern=None, secondary_result=None, all_results=results)

    ranked = sorted(detected.items(), key=lambda kv: kv[1]["quality"], reverse=True)
    primary_name, primary_result = ranked[0]
    secondary_name, secondary_result = (ranked[1] if len(ranked) > 1 else (None, None))
    return dict(primary_pattern=primary_name, primary_result=primary_result,
                secondary_pattern=secondary_name, secondary_result=secondary_result,
                all_results=results)
