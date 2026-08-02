"""Indicator, candle, swing and market-regime calculations.

All rolling calculations are backward-looking. No centred windows are used.
"""
from __future__ import annotations

from typing import Dict, List, Optional, Tuple

import numpy as np
import pandas as pd

from early_swing_models import MarketRegime


def normalise_ohlcv(df: pd.DataFrame) -> pd.DataFrame:
    if df is None or df.empty:
        return pd.DataFrame(columns=["Open", "High", "Low", "Close", "Volume"])
    out = df.copy()
    if isinstance(out.columns, pd.MultiIndex):
        # Works for a single ticker accidentally returned with a MultiIndex.
        if len(set(out.columns.get_level_values(-1))) == 1:
            out.columns = out.columns.get_level_values(0)
        else:
            out.columns = [str(c[0]) for c in out.columns]
    rename = {str(c).strip().lower(): c for c in out.columns}
    mapping = {}
    for wanted in ("open", "high", "low", "close", "volume"):
        if wanted in rename:
            mapping[rename[wanted]] = wanted.title()
    out = out.rename(columns=mapping)
    required = ["High", "Low", "Close", "Volume"]
    if any(c not in out.columns for c in required):
        return pd.DataFrame(columns=["Open", "High", "Low", "Close", "Volume"])
    if "Open" not in out.columns:
        out["Open"] = out["Close"].shift(1)
    out = out[["Open", "High", "Low", "Close", "Volume"]].apply(pd.to_numeric, errors="coerce")
    out = out.replace([np.inf, -np.inf], np.nan).dropna(subset=["High", "Low", "Close"])
    out["Volume"] = out["Volume"].fillna(0.0)
    out = out[~out.index.duplicated(keep="last")].sort_index()
    return out


def safe_div(numerator: float, denominator: float, default: Optional[float] = None) -> Optional[float]:
    if denominator is None or not np.isfinite(denominator) or abs(denominator) < 1e-12:
        return default
    if numerator is None or not np.isfinite(numerator):
        return default
    return float(numerator / denominator)


def ema(series: pd.Series, period: int) -> pd.Series:
    return series.ewm(span=period, adjust=False, min_periods=period).mean()


def sma(series: pd.Series, period: int) -> pd.Series:
    return series.rolling(period, min_periods=period).mean()


def rsi(series: pd.Series, period: int = 14) -> pd.Series:
    delta = series.diff()
    gain = delta.clip(lower=0.0)
    loss = -delta.clip(upper=0.0)
    avg_gain = gain.ewm(alpha=1 / period, adjust=False, min_periods=period).mean()
    avg_loss = loss.ewm(alpha=1 / period, adjust=False, min_periods=period).mean()
    rs = avg_gain / avg_loss.replace(0, np.nan)
    result = 100.0 - (100.0 / (1.0 + rs))
    return result.fillna(100.0).where(avg_gain.notna())


def true_range(df: pd.DataFrame) -> pd.Series:
    prev_close = df["Close"].shift(1)
    return pd.concat([
        df["High"] - df["Low"],
        (df["High"] - prev_close).abs(),
        (df["Low"] - prev_close).abs(),
    ], axis=1).max(axis=1)


def atr(df: pd.DataFrame, period: int = 14) -> pd.Series:
    return true_range(df).ewm(alpha=1 / period, adjust=False, min_periods=period).mean()


def adx(df: pd.DataFrame, period: int = 14) -> pd.Series:
    high, low = df["High"], df["Low"]
    up_move = high.diff()
    down_move = -low.diff()
    plus_dm = pd.Series(np.where((up_move > down_move) & (up_move > 0), up_move, 0.0), index=df.index)
    minus_dm = pd.Series(np.where((down_move > up_move) & (down_move > 0), down_move, 0.0), index=df.index)
    atr_s = true_range(df).ewm(alpha=1 / period, adjust=False, min_periods=period).mean()
    plus_di = 100 * plus_dm.ewm(alpha=1 / period, adjust=False, min_periods=period).mean() / atr_s
    minus_di = 100 * minus_dm.ewm(alpha=1 / period, adjust=False, min_periods=period).mean() / atr_s
    dx = 100 * (plus_di - minus_di).abs() / (plus_di + minus_di).replace(0, np.nan)
    return dx.ewm(alpha=1 / period, adjust=False, min_periods=period).mean()


def macd(series: pd.Series) -> Tuple[pd.Series, pd.Series, pd.Series]:
    line = ema(series, 12) - ema(series, 26)
    signal = line.ewm(span=9, adjust=False, min_periods=9).mean()
    return line, signal, line - signal


def pct_return(series: pd.Series, sessions: int, at_index: int = -1) -> Optional[float]:
    idx = len(series) + at_index if at_index < 0 else at_index
    prior = idx - sessions
    if prior < 0 or idx < 0 or idx >= len(series):
        return None
    base = float(series.iloc[prior])
    current = float(series.iloc[idx])
    return safe_div(current - base, base, None) * 100 if base else None


def candle_metrics(open_: float, high: float, low: float, close: float) -> Dict[str, float]:
    span = max(float(high - low), 1e-12)
    body_high = max(open_, close)
    body_low = min(open_, close)
    return {
        "clv": float((close - low) / span),
        "upper_wick_pct": float((high - body_high) / span * 100),
        "lower_wick_pct": float((body_low - low) / span * 100),
        "body_pct": float(abs(close - open_) / span * 100),
        "gap_pct": 0.0,
    }


def linear_slope_pct(series: pd.Series, window: int = 10) -> Optional[float]:
    values = series.dropna().iloc[-window:].to_numpy(dtype=float)
    if len(values) < window or np.any(values <= 0):
        return None
    x = np.arange(window, dtype=float)
    slope = np.polyfit(x, np.log(values), 1)[0]
    return float((np.exp(slope) - 1.0) * 100.0)


def local_swing_highs(values: pd.Series, order: int = 3) -> List[int]:
    arr = values.to_numpy(dtype=float)
    result: List[int] = []
    for idx in range(order, len(arr) - order):
        window = arr[idx - order: idx + order + 1]
        if np.isfinite(arr[idx]) and arr[idx] >= np.nanmax(window):
            result.append(idx)
    return result


def nearest_resistances(high: pd.Series, current_price: float, lookback: int = 252,
                        order: int = 3) -> List[float]:
    subset = high.iloc[-lookback:].reset_index(drop=True)
    levels = [float(subset.iloc[i]) for i in local_swing_highs(subset, order=order)
              if float(subset.iloc[i]) > current_price * 1.003]
    if not levels:
        max_high = float(subset.max()) if len(subset) else current_price
        if max_high > current_price * 1.003:
            levels.append(max_high)
    # Cluster levels within 0.75%; keep the lowest representative of each cluster.
    levels = sorted(levels)
    clustered: List[float] = []
    for level in levels:
        if not clustered or abs(level - clustered[-1]) / clustered[-1] > 0.0075:
            clustered.append(level)
    return clustered


def count_consecutive_expansion_candles(df: pd.DataFrame, atr_series: pd.Series) -> int:
    count = 0
    for idx in range(len(df) - 1, max(-1, len(df) - 8), -1):
        if idx <= 0:
            break
        row = df.iloc[idx]
        atr_value = atr_series.iloc[idx]
        if pd.isna(atr_value):
            break
        bullish = row["Close"] > row["Open"]
        large = (row["High"] - row["Low"]) >= 1.15 * atr_value
        if bullish and large:
            count += 1
        else:
            break
    return count


def relative_strength(stock_close: pd.Series, benchmark_close: pd.Series, sessions: int) -> Optional[float]:
    stock_ret = pct_return(stock_close, sessions)
    benchmark_ret = pct_return(benchmark_close, sessions)
    if stock_ret is None or benchmark_ret is None:
        return None
    return float(stock_ret - benchmark_ret)


def classify_market_regime(benchmark_df: pd.DataFrame) -> MarketRegime:
    df = normalise_ohlcv(benchmark_df)
    if len(df) < 60:
        return MarketRegime("UNKNOWN", 5.0, False, False, 0.0, 0.0)
    close = df["Close"]
    ema20 = ema(close, 20)
    sma50 = sma(close, 50)
    above20 = bool(close.iloc[-1] > ema20.iloc[-1])
    above50 = bool(close.iloc[-1] > sma50.iloc[-1])
    ret5 = pct_return(close, 5) or 0.0
    vol = float(close.pct_change().rolling(20).std().iloc[-1] * np.sqrt(252) * 100)
    slope20 = linear_slope_pct(ema20, 10) or 0.0
    if above20 and above50 and ret5 > 1 and slope20 > 0:
        classification, score = "STRONG_BULLISH", 12.0
    elif above50 and (above20 or slope20 > 0):
        classification, score = "MILD_BULLISH", 9.0
    elif vol > 35:
        classification, score = "HIGH_VOLATILITY", 4.0
    elif not above20 and not above50 and ret5 < -1:
        classification, score = "BEARISH", 1.0
    else:
        classification, score = "SIDEWAYS", 6.0
    return MarketRegime(classification, score, above20, above50, float(ret5), vol)
