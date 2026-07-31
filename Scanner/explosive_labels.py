"""
Historical outcome labels - used ONLY for calibration/backtesting, NEVER as
input to the score computed for "today". Section 11 + 12 (no look-ahead).

CRITICAL INVARIANT: given a DataFrame `df` and a signal day index `t`
(0-based, t is NOT the last row), these functions read ONLY df.iloc[t+1] -
the single next trading day - as the outcome. They never touch t+2 or
beyond, and they never touch anything at or before t (that's the SIGNAL's
job, computed by explosive_engine.py, which must itself never see t+1).
"""

from dataclasses import dataclass
from typing import Optional
import pandas as pd

from explosive_indicators import safe_div


@dataclass
class NextDayOutcome:
    next_day_high_return_pct: Optional[float]
    next_day_close_return_pct: Optional[float]
    next_day_max_adverse_excursion_pct: Optional[float]   # worst intraday drawdown from T's close
    next_day_volume_ratio: Optional[float]                # T+1 volume / recent median volume as of T
    explosive_intraday_success: Optional[bool]
    explosive_closing_success: Optional[bool]
    label_computable: bool
    reason_if_not: Optional[str]


def compute_next_day_outcome(df: pd.DataFrame, t: int, breakout_trigger: Optional[float],
                              atr_at_t: Optional[float], config, is_smallcap: bool = False) -> NextDayOutcome:
    """`df` must be indexed 0..N-1 (reset_index'd OHLCV). `t` is the signal
    day. Requires df to have at least t+1 rows (i.e. t is not the last row) -
    this is exactly what makes it usable for calibration (walking through
    HISTORICAL days where T+1 already happened) and unusable/inapplicable
    for "today" (where T+1 hasn't happened yet - callers must not call this
    for the live signal day)."""
    if t + 1 >= len(df):
        return NextDayOutcome(None, None, None, None, None, None, False,
                               "t+1 does not exist yet (this is 'today', not a historical day)")

    close_t = df["Close"].iloc[t]
    next_row = df.iloc[t + 1]
    if pd.isna(close_t) or pd.isna(next_row["High"]) or pd.isna(next_row["Low"]) or pd.isna(next_row["Close"]):
        return NextDayOutcome(None, None, None, None, None, None, False, "missing OHLC data at T or T+1")

    next_high_return = safe_div(next_row["High"] - close_t, close_t)
    next_high_return_pct = next_high_return * 100 if next_high_return is not None else None
    next_close_return = safe_div(next_row["Close"] - close_t, close_t)
    next_close_return_pct = next_close_return * 100 if next_close_return is not None else None
    mae = safe_div(close_t - next_row["Low"], close_t)
    mae_pct = max(0.0, mae * 100) if mae is not None else None

    # recent median volume AS OF T (not including T+1) - the correct
    # no-look-ahead baseline for "did T+1 volume expand"
    vol_window = df["Volume"].iloc[max(0, t - 19): t + 1]
    recent_median_vol = vol_window.median() if len(vol_window) >= 5 else None
    next_vol_ratio = safe_div(next_row["Volume"], recent_median_vol)

    min_high_pct = (config.smallcap_min_next_day_high_return_pct if is_smallcap
                    else config.min_next_day_high_return_pct)
    atr_pct_at_t = safe_div(atr_at_t, close_t) * 100 if (atr_at_t is not None and close_t) else None
    atr_based_threshold = (atr_pct_at_t * config.next_day_atr_multiplier) if atr_pct_at_t is not None else 0.0
    high_return_threshold = max(min_high_pct, atr_based_threshold)

    intraday_success = (next_high_return_pct is not None and next_high_return_pct >= high_return_threshold)

    close_above_trigger = (breakout_trigger is not None and next_row["Close"] >= breakout_trigger)
    close_success_by_return = (next_close_return_pct is not None
                                and next_close_return_pct >= config.min_next_day_close_return_pct)
    volume_confirms = (next_vol_ratio is not None and next_vol_ratio >= config.min_next_day_volume_ratio)

    explosive_intraday_success = bool(intraday_success and volume_confirms)
    explosive_closing_success = bool((close_success_by_return or close_above_trigger) and volume_confirms)

    return NextDayOutcome(
        next_day_high_return_pct=next_high_return_pct,
        next_day_close_return_pct=next_close_return_pct,
        next_day_max_adverse_excursion_pct=mae_pct,
        next_day_volume_ratio=next_vol_ratio,
        explosive_intraday_success=explosive_intraday_success,
        explosive_closing_success=explosive_closing_success,
        label_computable=True, reason_if_not=None,
    )
