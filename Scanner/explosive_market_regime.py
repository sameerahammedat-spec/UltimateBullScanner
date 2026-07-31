"""
Market/benchmark regime classification (Section 4).

Uses whichever benchmark history is available (Nifty 50 for NSE names,
Sensex/BSE 500 for BSE names) - the CALLER passes in the right benchmark
close series; this module doesn't know or care which index it is.

Sector-index relative strength (also mentioned in Section 4) is NOT
implemented: there's no reliable free sector-index data source for Indian
equities that I can respect the same "no fabricated data" rule with, so
"sector RS" is left out entirely rather than faked from a made-up mapping.
"""

from dataclasses import dataclass
from typing import Optional
import numpy as np
import pandas as pd

from explosive_indicators import safe_div, series_slope_pct_per_day

REGIME_STRONG_BULLISH = "STRONG_BULLISH"
REGIME_BULLISH = "BULLISH"
REGIME_NEUTRAL = "NEUTRAL"
REGIME_WEAK = "WEAK"
REGIME_RISK_OFF = "RISK_OFF"


@dataclass
class MarketRegime:
    classification: str
    above_20dma: Optional[bool]
    above_50dma: Optional[bool]
    dma20_slope_pct_per_day: Optional[float]
    five_day_return_pct: Optional[float]
    breadth_pct_above_50dma: Optional[float]   # % of the SCANNED universe above its own 50DMA
    score_component: float                      # 0-10, feeds directly into the Explosive Score


def classify_regime(benchmark_close: pd.Series, universe_above_50dma_flags: list) -> MarketRegime:
    """`benchmark_close` = the index's close series, most recent last.
    `universe_above_50dma_flags` = list[bool] of whether EACH scanned stock
    is above its own 50DMA today - this is breadth, computed for free from
    data the scanners already collected (no extra fetch)."""
    n = len(benchmark_close)
    if n < 50:
        return MarketRegime(REGIME_NEUTRAL, None, None, None, None, None, 5.0)

    last = benchmark_close.iloc[-1]
    dma20 = benchmark_close.rolling(20).mean().iloc[-1]
    dma50 = benchmark_close.rolling(50).mean().iloc[-1]
    above_20 = bool(last > dma20) if pd.notna(dma20) else None
    above_50 = bool(last > dma50) if pd.notna(dma50) else None
    slope20 = series_slope_pct_per_day(benchmark_close.rolling(20).mean().dropna(), window=10)
    five_day_ret = safe_div(last - benchmark_close.iloc[-6], benchmark_close.iloc[-6]) * 100 if n > 6 else None

    breadth = (float(np.mean(universe_above_50dma_flags)) * 100) if universe_above_50dma_flags else None

    bullish_votes = sum([above_20 is True, above_50 is True, (slope20 or 0) > 0, (five_day_ret or 0) > 0])
    bearish_votes = sum([above_20 is False, above_50 is False, (slope20 or 0) < 0, (five_day_ret or 0) < -1])

    if bullish_votes >= 4:
        classification, score = REGIME_STRONG_BULLISH, 10.0
    elif bullish_votes >= 3:
        classification, score = REGIME_BULLISH, 8.0
    elif bearish_votes >= 3:
        classification, score = REGIME_RISK_OFF, 1.0
    elif bearish_votes >= 2:
        classification, score = REGIME_WEAK, 4.0
    else:
        classification, score = REGIME_NEUTRAL, 6.0

    return MarketRegime(classification, above_20, above_50, slope20, five_day_ret, breadth, score)
