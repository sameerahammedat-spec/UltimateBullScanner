"""
New indicators needed by the Explosive Move Engine that don't already exist
in market_scanner.py. RSI(14), ATR(14), ADX(14), and the 20-day trend
regression slope/R^2 already live there and are imported from there, not
duplicated here (Section 1: "identify duplicated indicator logic").

Every function here takes a plain OHLCV DataFrame (columns: Open, High, Low,
Close, Volume) sliced to end at whatever day is being analyzed - callers are
responsible for the no-look-ahead-bias slicing (Section 12); nothing in this
file peeks at rows beyond what it's given.
"""

from typing import Optional
import numpy as np
import pandas as pd


def safe_div(numerator: float, denominator: float) -> Optional[float]:
    """Zero/near-zero-denominator-safe division. Returns None (never 0 or
    inf) when the denominator is unusable - callers must handle None
    explicitly rather than silently treating it as a real zero value."""
    if denominator is None or numerator is None:
        return None
    if abs(denominator) < 1e-9:
        return None
    return numerator / denominator


def ema(series: pd.Series, span: int) -> pd.Series:
    return series.ewm(span=span, adjust=False).mean()


def sma(series: pd.Series, window: int) -> pd.Series:
    return series.rolling(window).mean()


def series_slope_pct_per_day(series: pd.Series, window: int = 10) -> Optional[float]:
    """% change per day of a moving-average series (e.g. EMA20 or SMA50)
    over the last `window` bars, via log-linear regression - same technique
    as market_scanner.trend_regression, applied to an MA series instead of
    raw close."""
    y = series.iloc[-window:].values
    if len(y) < window or np.any(pd.isna(y)) or np.any(y <= 0):
        return None
    log_y = np.log(y)
    x = np.arange(window, dtype=float)
    slope, _ = np.polyfit(x, log_y, 1)
    return float((np.exp(slope) - 1) * 100)


def ma_alignment(close_last: float, ema10_last: float, ema20_last: float,
                  sma50_last: float, sma100_last: Optional[float],
                  sma200_last: Optional[float]) -> str:
    """Simple bullish-alignment label: are shorter MAs stacked above longer
    ones, with price on top? This is a supporting signal, not a detector on
    its own."""
    vals = [v for v in [ema10_last, ema20_last, sma50_last, sma100_last, sma200_last] if v is not None]
    if len(vals) < 3:
        return "INSUFFICIENT"
    stacked = all(vals[i] >= vals[i + 1] * 0.995 for i in range(len(vals) - 1))  # small tolerance
    price_on_top = close_last >= vals[0] * 0.98
    if stacked and price_on_top:
        return "BULLISH_ALIGNED"
    elif stacked:
        return "ALIGNED_BUT_PRICE_BELOW"
    return "NOT_ALIGNED"


def rate_of_change(close: pd.Series, periods: int) -> Optional[float]:
    if len(close) <= periods:
        return None
    return safe_div(close.iloc[-1] - close.iloc[-1 - periods], close.iloc[-1 - periods])


def bollinger_width(close: pd.Series, window: int = 20, num_std: float = 2.0) -> Optional[float]:
    """Bollinger Band width as % of the middle band - a compression measure."""
    if len(close) < window:
        return None
    window_slice = close.iloc[-window:]
    mid = window_slice.mean()
    std = window_slice.std()
    if mid is None or abs(mid) < 1e-9:
        return None
    upper, lower = mid + num_std * std, mid - num_std * std
    return float((upper - lower) / mid * 100)


def bollinger_width_percentile(close: pd.Series, window: int = 20, lookback: int = 120) -> Optional[float]:
    """Where today's Bollinger width sits vs its own history over `lookback`
    days - a LOW percentile means volatility is unusually contracted right
    now relative to this specific stock's own normal range (Section 6)."""
    if len(close) < window + lookback:
        lookback = len(close) - window
        if lookback < 20:
            return None
    widths = []
    for i in range(len(close) - lookback, len(close) + 1):
        if i < window:
            continue
        w = bollinger_width(close.iloc[:i], window=window)
        if w is not None:
            widths.append(w)
    if len(widths) < 10:
        return None
    current = widths[-1]
    pct = float(np.mean(np.array(widths[:-1]) <= current) * 100)
    return pct


def atr_pct(atr_value: Optional[float], close_value: Optional[float]) -> Optional[float]:
    return safe_div(atr_value, close_value) * 100 if (atr_value is not None and close_value) else None


def range_compression_ratio(high: pd.Series, low: pd.Series, recent: int = 5, baseline: int = 20) -> Optional[float]:
    """Recent N-day high-low range vs the prior baseline-day range - a
    ratio below 1 means recent price action has genuinely tightened, not
    just that ATR (which lags) has drifted down."""
    if len(high) < baseline:
        return None
    recent_range = (high.iloc[-recent:] - low.iloc[-recent:]).mean()
    baseline_range = (high.iloc[-baseline:-recent] - low.iloc[-baseline:-recent]).mean() if baseline > recent else None
    return safe_div(recent_range, baseline_range)


def gap_frequency(open_: pd.Series, close: pd.Series, lookback: int = 60, gap_threshold_pct: float = 2.0) -> dict:
    """% of sessions in the lookback window that opened with a gap (up or
    down) beyond gap_threshold_pct vs the prior close - frequent large gaps
    make ATR-based stop placement less reliable."""
    if len(open_) < 2 or len(close) < 2:
        return dict(gap_up_pct=None, gap_down_pct=None)
    n = min(lookback, len(open_) - 1)
    prev_close = close.shift(1).iloc[-n:]
    today_open = open_.iloc[-n:]
    gap_pct = (today_open - prev_close) / prev_close * 100
    gap_pct = gap_pct.dropna()
    if len(gap_pct) == 0:
        return dict(gap_up_pct=None, gap_down_pct=None)
    gap_up_pct = float((gap_pct > gap_threshold_pct).mean() * 100)
    gap_down_pct = float((gap_pct < -gap_threshold_pct).mean() * 100)
    return dict(gap_up_pct=gap_up_pct, gap_down_pct=gap_down_pct)


def volume_dry_up_ratio(volume: pd.Series) -> Optional[float]:
    """5-day median volume / 20-day median volume - below 1 means recent
    volume has genuinely dried up relative to the base, a classic
    pre-breakout signature (Section 6)."""
    if len(volume) < 20:
        return None
    med5 = volume.iloc[-5:].median()
    med20 = volume.iloc[-20:].median()
    return safe_div(med5, med20)


def obv_slope(close: pd.Series, volume: pd.Series, window: int = 10) -> Optional[float]:
    """On-Balance-Volume slope over the last `window` bars - a simple
    accumulation/distribution proxy: is volume net flowing in on up days
    more than out on down days, and is that trend RISING."""
    if len(close) < window + 1:
        return None
    direction = np.sign(close.diff().fillna(0.0).values)
    obv = np.cumsum(direction * volume.values)
    y = obv[-window:]
    x = np.arange(window, dtype=float)
    if np.std(y) < 1e-9:
        return 0.0
    slope, _ = np.polyfit(x, y, 1)
    # normalize by the average absolute OBV level so this is comparable across stocks
    scale = np.mean(np.abs(y)) or 1.0
    return float(slope / scale)


def up_down_volume_ratio(close: pd.Series, volume: pd.Series, window: int = 20) -> Optional[float]:
    """Total volume on up-days / total volume on down-days over the window -
    used for the distribution-volume penalty (heavy volume on RED days near
    resistance is a bearish tell, Section 9)."""
    if len(close) < window + 1:
        return None
    rets = close.diff().iloc[-window:]
    vol = volume.iloc[-window:]
    up_vol = vol[rets > 0].sum()
    down_vol = vol[rets < 0].sum()
    return safe_div(up_vol, down_vol)


def candle_quality(open_v: float, high_v: float, low_v: float, close_v: float) -> dict:
    """Body %, upper/lower wick %, and close-location-value (0=at the low,
    1=at the high) for a single candle - all safe-denominator (Section 6)."""
    rng = high_v - low_v
    if rng is None or rng < 1e-9:
        return dict(body_pct=None, upper_wick_pct=None, lower_wick_pct=None, clv=None)
    body_pct = abs(close_v - open_v) / rng * 100
    upper_wick_pct = (high_v - max(open_v, close_v)) / rng * 100
    lower_wick_pct = (min(open_v, close_v) - low_v) / rng * 100
    clv = (close_v - low_v) / rng   # 0..1
    return dict(body_pct=float(body_pct), upper_wick_pct=float(upper_wick_pct),
                lower_wick_pct=float(lower_wick_pct), clv=float(clv))


def distance_from_high(close_last: float, high_series: pd.Series, lookback: int) -> Optional[float]:
    """% below the highest HIGH in the last `lookback` sessions (positive = below)."""
    if len(high_series) < 1:
        return None
    n = min(lookback, len(high_series))
    peak = high_series.iloc[-n:].max()
    return safe_div(peak - close_last, peak) * 100 if peak else None


def inside_bar(high: pd.Series, low: pd.Series) -> bool:
    """Today's range entirely inside yesterday's range."""
    if len(high) < 2:
        return False
    return bool(high.iloc[-1] <= high.iloc[-2] and low.iloc[-1] >= low.iloc[-2])


def narrow_range_n(high: pd.Series, low: pd.Series, n: int) -> bool:
    """Today's range is the narrowest of the last n sessions (NR4/NR7)."""
    if len(high) < n:
        return False
    ranges = (high - low).iloc[-n:]
    return bool(ranges.iloc[-1] <= ranges.min() + 1e-9)


def range_percentile(high: pd.Series, low: pd.Series, lookback: int) -> Optional[float]:
    """Where today's range sits vs its own history - low percentile = compressed."""
    if len(high) < lookback:
        lookback = len(high)
    if lookback < 10:
        return None
    ranges = (high - low).iloc[-lookback:]
    today = ranges.iloc[-1]
    return float((ranges <= today).mean() * 100)
