"""
Chart pattern detector using OpenCV + scipy for swing-point geometry.

WHAT THIS ACTUALLY DOES (read before trusting it):
This is geometric pattern-matching on price/volume history, not a trained
machine-learning model and not a guarantee of what happens next. It:
  1. Finds swing highs/lows over the recent lookback window (scipy)
  2. Fits robust trendlines through those swing points using OpenCV's
     cv2.fitLine (a real, standard CV line-fitting routine) to get the
     slope and how well the swings actually sit on that line (R^2)
  3. Classifies the shape into a handful of well-known setups, OR flags it
     as a "Blow-off Spike" - which is exactly the pattern behind the "gains
     14% in a day, gives it all back in a week" behavior you're trying to
     screen OUT.

This is a heuristic screen to prioritize your attention, not an oracle.
Two different pros looking at the same chart can reasonably disagree on
what pattern it is - treat the label as "worth a manual look", not gospel.

Requires: opencv-python-headless, scipy, numpy
    pip install opencv-python-headless scipy
"""

import numpy as np
import cv2
from scipy.signal import argrelextrema

BULLISH_PATTERNS = {"Ascending Triangle Breakout", "Cup and Handle", "Bull Flag Breakout", "Range Breakout"}
AVOID_PATTERNS = {"Blow-off Spike (caution)"}


def _swing_points(values, order=3):
    """Local maxima/minima indices using a simple window comparison (scipy)."""
    highs_idx = argrelextrema(values, np.greater_equal, order=order)[0]
    lows_idx = argrelextrema(values, np.less_equal, order=order)[0]
    # de-duplicate consecutive equal-value plateaus
    highs_idx = np.array(sorted(set(highs_idx.tolist())))
    lows_idx = np.array(sorted(set(lows_idx.tolist())))
    return highs_idx, lows_idx


def _fit_trendline_cv(x, y):
    """Robust line fit through points using OpenCV's cv2.fitLine
    (DIST_L2 estimator). Returns (slope, intercept, r_squared).
    x, y are 1D numpy arrays of equal length; need >= 2 points."""
    if len(x) < 2:
        return None, None, None
    pts = np.column_stack([x.astype(np.float32), y.astype(np.float32)])
    vx, vy, x0, y0 = cv2.fitLine(pts, cv2.DIST_L2, 0, 0.01, 0.01).flatten()
    if abs(vx) < 1e-9:
        return None, None, None
    slope = vy / vx
    intercept = y0 - slope * x0
    y_hat = slope * x + intercept
    ss_res = np.sum((y - y_hat) ** 2)
    ss_tot = np.sum((y - np.mean(y)) ** 2)
    r2 = 1 - ss_res / ss_tot if ss_tot > 1e-9 else 0.0
    return float(slope), float(intercept), float(r2)


def _fit_parabola(x, y):
    """For cup-shape detection: fit degree-2 polynomial, return (a, b, c, r2).
    a > 0 means U-shaped (cup); a < 0 means inverted-U (dome, not a cup)."""
    if len(x) < 5:
        return None, None, None, None
    coeffs = np.polyfit(x, y, 2)
    a, b, c = coeffs
    y_hat = np.polyval(coeffs, x)
    ss_res = np.sum((y - y_hat) ** 2)
    ss_tot = np.sum((y - np.mean(y)) ** 2)
    r2 = 1 - ss_res / ss_tot if ss_tot > 1e-9 else 0.0
    return float(a), float(b), float(c), float(r2)


def render_price_chart_image(close, width=640, height=320):
    """Draws the close-price series onto a numpy image using OpenCV
    polylines. Not used for pattern math directly (that's done on the raw
    numbers above) - this is for optional visual sanity-checking, e.g.
    saving a .png per candidate to eyeball later. Returns a BGR image array."""
    n = len(close)
    img = np.full((height, width, 3), 255, dtype=np.uint8)
    lo, hi = float(np.min(close)), float(np.max(close))
    span = (hi - lo) or 1.0
    xs = np.linspace(20, width - 20, n).astype(np.int32)
    ys = (height - 20 - (close - lo) / span * (height - 40)).astype(np.int32)
    pts = np.column_stack([xs, ys]).reshape(-1, 1, 2)
    cv2.polylines(img, [pts], isClosed=False, color=(31, 56, 31), thickness=2, lineType=cv2.LINE_AA)
    return img


def detect_pattern(close, volume, lookback=60, swing_order=3, min_gain_for_spike_check=5.0):
    """Main entry point. `close` and `volume` are 1D numpy arrays (or pandas
    Series -> .values), most recent value LAST. Returns a dict:
        pattern, confidence (0-100), is_bullish, is_spike_warning, detail
    """
    close = np.asarray(close, dtype=float)
    volume = np.asarray(volume, dtype=float)
    if len(close) < lookback:
        lookback = len(close)
    c = close[-lookback:]
    v = volume[-lookback:]
    n = len(c)
    x = np.arange(n, dtype=float)

    today_ret_pct = (c[-1] / c[-2] - 1) * 100 if n > 1 else 0.0
    daily_rets = np.diff(c) / c[:-1] * 100
    ret_std = np.std(daily_rets[:-1]) if len(daily_rets) > 2 else None
    ret_zscore = (today_ret_pct - np.mean(daily_rets[:-1])) / ret_std if (ret_std and ret_std > 1e-6) else None

    highs_idx, lows_idx = _swing_points(c, order=swing_order)

    # --- 1. Blow-off spike check (what you're trying to screen OUT) -----
    # A spike is: today's move is a statistical outlier vs its own recent
    # volatility, AND there was no real base/trend building beforehand
    # (few/no clean swing points, or a flat-to-declining trend just before).
    pre_spike = c[:-1]
    pre_slope, _, pre_r2 = _fit_trendline_cv(x[:-1], pre_spike) if n > 5 else (None, None, None)
    looks_like_spike = (
        ret_zscore is not None and ret_zscore > 3.0 and
        today_ret_pct >= min_gain_for_spike_check and
        (pre_r2 is None or pre_r2 < 0.3 or (pre_slope is not None and pre_slope <= 0))
    )
    if looks_like_spike:
        return dict(pattern="Blow-off Spike (caution)", confidence=round(min(95, 50 + (ret_zscore or 0) * 10), 1),
                     is_bullish=False, is_spike_warning=True,
                     detail=f"Today's move is a {ret_zscore:.1f}-sigma outlier with little prior base "
                            f"(pre-move trend R^2={pre_r2 if pre_r2 is not None else 'N/A'}). "
                            f"This is the classic shape of a move that doesn't hold - single-day spike, "
                            f"no accumulation beforehand.")

    # --- 2. Ascending Triangle: flat-ish resistance, rising support ------
    resistance_slope = resistance_r2 = support_slope = support_r2 = None
    if len(highs_idx) >= 2:
        resistance_slope, _, resistance_r2 = _fit_trendline_cv(x[highs_idx], c[highs_idx])
    if len(lows_idx) >= 2:
        support_slope, _, support_r2 = _fit_trendline_cv(x[lows_idx], c[lows_idx])

    price_scale = np.mean(c)
    flat_thresh = 0.0015 * price_scale  # slope considered "flat" if < 0.15%/day of price level

    if (resistance_slope is not None and support_slope is not None and
            abs(resistance_slope) < flat_thresh and support_slope > flat_thresh * 0.5 and
            resistance_r2 is not None and resistance_r2 > 0.5 and support_r2 > 0.5 and
            c[-1] >= 0.98 * np.max(c[highs_idx])):
        conf = round(min(95, 50 + 25 * resistance_r2 + 25 * support_r2), 1)
        return dict(pattern="Ascending Triangle Breakout", confidence=conf, is_bullish=True, is_spike_warning=False,
                     detail=f"Resistance flat (slope~{resistance_slope:.3f}, R^2={resistance_r2:.2f}), "
                            f"support rising (slope~{support_slope:.3f}, R^2={support_r2:.2f}), price near/above resistance.")

    # --- 3. Cup and Handle: U-shape then small pullback then breakout ----
    a, b, cc, cup_r2 = _fit_parabola(x, c)
    if a is not None and a > 0 and cup_r2 > 0.5:
        handle_window = max(5, n // 8)
        handle = c[-handle_window:]
        handle_slope, _, handle_r2 = _fit_trendline_cv(np.arange(len(handle), dtype=float), handle)
        cup_rim = max(c[: n // 3].max() if n >= 6 else c.max(), c[-1])
        if (handle_slope is not None and handle_slope <= 0 and c[-1] >= 0.97 * cup_rim):
            conf = round(min(95, 45 + 35 * cup_r2), 1)
            return dict(pattern="Cup and Handle", confidence=conf, is_bullish=True, is_spike_warning=False,
                         detail=f"U-shaped recovery (parabola fit R^2={cup_r2:.2f}) with a shallow pullback "
                                f"(handle slope~{handle_slope:.3f}) near the prior high.")

    # --- 4. Bull Flag: sharp prior rally + tight consolidation + breakout -
    if n >= 20:
        pole = c[: n // 2]
        flag = c[n // 2:]
        pole_slope, _, pole_r2 = _fit_trendline_cv(np.arange(len(pole), dtype=float), pole)
        flag_range_pct = (np.max(flag) - np.min(flag)) / np.mean(flag) * 100 if np.mean(flag) else 999
        if (pole_slope is not None and pole_slope > flat_thresh and pole_r2 > 0.5 and
                flag_range_pct < 8 and c[-1] >= np.max(flag[:-1] if len(flag) > 1 else flag)):
            conf = round(min(95, 45 + 30 * pole_r2 + (8 - flag_range_pct) * 2), 1)
            return dict(pattern="Bull Flag Breakout", confidence=conf, is_bullish=True, is_spike_warning=False,
                         detail=f"Strong prior rally (slope~{pole_slope:.3f}, R^2={pole_r2:.2f}) followed by a "
                                f"tight {flag_range_pct:.1f}% consolidation, breaking out today.")

    # --- 5. Plain range breakout: tight recent base + today's push above -
    base_window = c[:-1][-max(10, n // 4):]
    base_range_pct = (np.max(base_window) - np.min(base_window)) / np.mean(base_window) * 100 if len(base_window) else 999
    vol_ratio = (v[-1] / np.mean(v[-6:-1])) if len(v) > 6 and np.mean(v[-6:-1]) > 0 else None
    if base_range_pct < 6 and c[-1] > np.max(base_window) and vol_ratio and vol_ratio > 1.3:
        conf = round(min(90, 40 + (6 - base_range_pct) * 5 + min(20, (vol_ratio - 1) * 20)), 1)
        return dict(pattern="Range Breakout", confidence=conf, is_bullish=True, is_spike_warning=False,
                     detail=f"Tight {base_range_pct:.1f}% base broken today on {vol_ratio:.1f}x volume.")

    return dict(pattern="No Clear Pattern", confidence=30.0, is_bullish=False, is_spike_warning=False,
                 detail="No recognizable base/breakout/cup/flag geometry in the lookback window.")
