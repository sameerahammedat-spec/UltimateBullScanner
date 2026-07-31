"""
Objective swing-point detection and support/resistance quality scoring.

Uses fractal pivots (a bar is a swing high if it's the highest of `order`
bars on each side) rather than single-candle pivots, which are too noisy
(Section 7). All tolerances scale with ATR% so the same logic works for a
stable large-cap and a volatile micro-cap without separate tuning.
"""

from dataclasses import dataclass
from typing import List, Optional
import numpy as np
import pandas as pd


@dataclass
class SwingPoint:
    index: int      # position within the array passed in (0-based, most recent = len-1)
    price: float
    is_high: bool


def find_swings(high: np.ndarray, low: np.ndarray, order: int = 3) -> List[SwingPoint]:
    """Fractal swing detection: index i is a swing high if high[i] is the
    max of high[i-order : i+order+1]; symmetric for swing lows. Excludes
    the last `order` bars from being confirmed swings (a swing needs bars
    AFTER it to confirm - which is exactly the no-look-ahead-safe behavior:
    a swing detected at bar i only ever used data up to i+order, and
    callers analyzing "as of day T" must only pass data through day T, so
    the most recent `order` bars simply won't have confirmed swings yet -
    that's correct behavior, not a bug.
    """
    n = len(high)
    swings: List[SwingPoint] = []
    for i in range(order, n - order):
        window_high = high[i - order: i + order + 1]
        window_low = low[i - order: i + order + 1]
        if high[i] == window_high.max() and np.argmax(window_high) == order:
            swings.append(SwingPoint(index=i, price=float(high[i]), is_high=True))
        if low[i] == window_low.min() and np.argmin(window_low) == order:
            swings.append(SwingPoint(index=i, price=float(low[i]), is_high=False))
    return swings


@dataclass
class LevelQuality:
    price: float
    touches: int
    quality: float          # 0-100
    most_recent_index: int


def resistance_quality(swings: List[SwingPoint], current_index: int, atr_value: Optional[float],
                        tol_atr_mult: float = 0.5, recency_window: int = 40) -> Optional[LevelQuality]:
    """Groups swing HIGHS that are within `tol_atr_mult` ATRs of each other
    into a single resistance level (never exact equality - Section 5C/7),
    scores by touch count, recency, and closeness of touches. Returns the
    single best (most touched, most recent) resistance level, or None if
    no swing highs are available yet."""
    highs = [s for s in swings if s.is_high]
    if not highs or atr_value is None or atr_value <= 0:
        return None

    tol = atr_value * tol_atr_mult
    clusters: List[List[SwingPoint]] = []
    for s in sorted(highs, key=lambda x: x.price):
        placed = False
        for cluster in clusters:
            if abs(cluster[-1].price - s.price) <= tol:
                cluster.append(s)
                placed = True
                break
        if not placed:
            clusters.append([s])

    best = None
    for cluster in clusters:
        touches = len(cluster)
        avg_price = float(np.mean([c.price for c in cluster]))
        most_recent = max(c.index for c in cluster)
        recency_bonus = max(0.0, 1.0 - (current_index - most_recent) / recency_window) * 30
        touch_bonus = min(touches, 4) * 15   # diminishing returns past 4 touches
        tightness = 1.0 - (np.std([c.price for c in cluster]) / avg_price if avg_price else 1.0)
        tightness_bonus = max(0.0, tightness) * 25
        quality = min(100.0, touch_bonus + recency_bonus + tightness_bonus)
        cand = LevelQuality(price=avg_price, touches=touches, quality=quality, most_recent_index=most_recent)
        if best is None or cand.quality > best.quality:
            best = cand
    return best


def support_quality(swings: List[SwingPoint], current_index: int, atr_value: Optional[float],
                     tol_atr_mult: float = 0.5, recency_window: int = 40) -> Optional[LevelQuality]:
    """Mirror of resistance_quality() for swing LOWS - number of successful
    holds, recency, ATR-adjusted closeness (Section 7)."""
    lows = [s for s in swings if not s.is_high]
    if not lows or atr_value is None or atr_value <= 0:
        return None
    tol = atr_value * tol_atr_mult
    clusters: List[List[SwingPoint]] = []
    for s in sorted(lows, key=lambda x: x.price):
        placed = False
        for cluster in clusters:
            if abs(cluster[-1].price - s.price) <= tol:
                cluster.append(s)
                placed = True
                break
        if not placed:
            clusters.append([s])

    best = None
    for cluster in clusters:
        touches = len(cluster)
        avg_price = float(np.mean([c.price for c in cluster]))
        most_recent = max(c.index for c in cluster)
        recency_bonus = max(0.0, 1.0 - (current_index - most_recent) / recency_window) * 30
        touch_bonus = min(touches, 4) * 15
        tightness = 1.0 - (np.std([c.price for c in cluster]) / avg_price if avg_price else 1.0)
        tightness_bonus = max(0.0, tightness) * 25
        quality = min(100.0, touch_bonus + recency_bonus + tightness_bonus)
        cand = LevelQuality(price=avg_price, touches=touches, quality=quality, most_recent_index=most_recent)
        if best is None or cand.quality > best.quality:
            best = cand
    return best


def nearest_resistance_above(swings: List[SwingPoint], price: float) -> Optional[float]:
    """The lowest swing-high price that sits ABOVE the given price - used
    for 'available upside before resistance' (Section 17)."""
    candidates = [s.price for s in swings if s.is_high and s.price > price]
    return min(candidates) if candidates else None
