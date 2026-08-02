"""Pooled empirical calibration for the Early Swing Rally Scanner.

This module never converts the rules score directly into a probability. It uses
chronological out-of-sample backtest trades and reports empirical hit rates only
when the sample size is adequate. Candidate lookup uses a documented fallback
hierarchy from specific buckets to broader score buckets.
"""
from __future__ import annotations

import json
import math
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, Iterable, List, Mapping, Optional, Sequence, Tuple

import pandas as pd

from early_swing_backtest import score_band
from early_swing_config import DEFAULT_EARLY_SWING_CONFIG, EarlySwingConfig
from early_swing_models import BacktestTrade, CalibrationEstimate, EarlySwingCandidate

CALIBRATION_VERSION = 1


def atr_bucket(value: Optional[float]) -> str:
    if value is None:
        return "UNKNOWN"
    value = float(value)
    if value < 2.0:
        return "LOW"
    if value < 4.0:
        return "MEDIUM"
    if value < 7.0:
        return "HIGH"
    return "VERY_HIGH"


def wilson_interval(successes: int, total: int, z: float = 1.96) -> Tuple[Optional[float], Optional[float]]:
    if total <= 0:
        return None, None
    p = successes / total
    denominator = 1 + z * z / total
    centre = (p + z * z / (2 * total)) / denominator
    margin = z * math.sqrt((p * (1 - p) + z * z / (4 * total)) / total) / denominator
    return max(0.0, (centre - margin) * 100), min(100.0, (centre + margin) * 100)


def sample_confidence(sample_size: int, config: EarlySwingConfig) -> str:
    if sample_size < config.calibration_minimum_sample:
        return "INSUFFICIENT_SAMPLE"
    if sample_size < config.calibration_medium_sample:
        return "LOW"
    if sample_size < config.calibration_high_sample:
        return "MEDIUM"
    return "HIGH"


def _key(parts: Sequence[Tuple[str, object]]) -> str:
    return "|".join(f"{name}={value}" for name, value in parts)


def trade_bucket_keys(trade: BacktestTrade) -> List[Tuple[str, str]]:
    """Return bucket keys ordered from most specific to broadest."""
    exact = _key((
        ("setup", trade.setup_type),
        ("score", trade.score_band),
        ("age", trade.rally_age),
        ("regime", trade.market_regime),
        ("atr", atr_bucket(trade.atr_pct)),
    ))
    return [
        ("EXACT", exact),
        ("SETUP_SCORE_AGE_REGIME", _key((
            ("setup", trade.setup_type), ("score", trade.score_band),
            ("age", trade.rally_age), ("regime", trade.market_regime),
        ))),
        ("SETUP_SCORE_REGIME", _key((
            ("setup", trade.setup_type), ("score", trade.score_band),
            ("regime", trade.market_regime),
        ))),
        ("SETUP_SCORE", _key((("setup", trade.setup_type), ("score", trade.score_band)))),
        ("SCORE_REGIME", _key((("score", trade.score_band), ("regime", trade.market_regime)))),
        ("SCORE_ONLY", _key((("score", trade.score_band),))),
        ("GLOBAL", "GLOBAL"),
    ]


def candidate_bucket_keys(candidate: EarlySwingCandidate,
                          config: EarlySwingConfig = DEFAULT_EARLY_SWING_CONFIG) -> List[Tuple[str, str]]:
    band = score_band(candidate.rules_score, config)
    exact = _key((
        ("setup", candidate.setup_type),
        ("score", band),
        ("age", int(candidate.rally_age or 0)),
        ("regime", candidate.market_regime),
        ("atr", atr_bucket(candidate.atr_pct)),
    ))
    return [
        ("EXACT", exact),
        ("SETUP_SCORE_AGE_REGIME", _key((
            ("setup", candidate.setup_type), ("score", band),
            ("age", int(candidate.rally_age or 0)), ("regime", candidate.market_regime),
        ))),
        ("SETUP_SCORE_REGIME", _key((
            ("setup", candidate.setup_type), ("score", band),
            ("regime", candidate.market_regime),
        ))),
        ("SETUP_SCORE", _key((("setup", candidate.setup_type), ("score", band)))),
        ("SCORE_REGIME", _key((("score", band), ("regime", candidate.market_regime)))),
        ("SCORE_ONLY", _key((("score", band),))),
        ("GLOBAL", "GLOBAL"),
    ]


def _bucket_stats(trades: Sequence[BacktestTrade], level: str, key: str) -> dict:
    total = len(trades)
    successes = sum(trade.result == "TARGET" for trade in trades)
    hit_rate = successes / total * 100 if total else None
    ci_low, ci_high = wilson_interval(successes, total)
    returns = [float(trade.return_pct) for trade in trades]
    return {
        "level": level,
        "key": key,
        "sample_size": total,
        "successes": successes,
        "hit_rate_pct": hit_rate,
        "ci_low_pct": ci_low,
        "ci_high_pct": ci_high,
        "average_return_pct": sum(returns) / total if total else None,
        "average_mfe_pct": sum(float(t.max_favourable_excursion_pct) for t in trades) / total if total else None,
        "average_mae_pct": sum(float(t.max_adverse_excursion_pct) for t in trades) / total if total else None,
        "false_breakout_rate_pct": sum(t.result == "STOP" for t in trades) / total * 100 if total else None,
    }


def build_calibration(
    trades: Sequence[BacktestTrade],
    config: EarlySwingConfig = DEFAULT_EARLY_SWING_CONFIG,
    entry_method: str = "NEXT_OPEN",
    target_method: str = "FIXED_PERCENT",
    holding_limit: Optional[int] = None,
) -> dict:
    """Build pooled buckets from one canonical, comparable trade definition."""
    selected_holding = config.backtest_holding_sessions if holding_limit is None else int(holding_limit)
    selected = [
        trade for trade in trades
        if trade.entry_method.upper() == entry_method.upper()
        and trade.target_method.upper() == target_method.upper()
        and int(trade.holding_limit) == selected_holding
    ]

    grouped: Dict[Tuple[str, str], List[BacktestTrade]] = {}
    for trade in selected:
        for level, key in trade_bucket_keys(trade):
            grouped.setdefault((level, key), []).append(trade)

    buckets = [_bucket_stats(group, level, key) for (level, key), group in grouped.items()]
    buckets.sort(key=lambda row: (row["level"], row["key"]))
    return {
        "version": CALIBRATION_VERSION,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "definition": {
            "success": "TARGET_REACHED_BEFORE_STOP",
            "entry_method": entry_method.upper(),
            "target_method": target_method.upper(),
            "holding_limit": selected_holding,
            "target_pct": config.backtest_target_pct,
            "slippage_pct": config.backtest_slippage_pct,
            "cost_pct": config.backtest_cost_pct,
        },
        "minimum_sample": config.calibration_minimum_sample,
        "selected_trade_count": len(selected),
        "buckets": buckets,
    }


def save_calibration(calibration: Mapping[str, object], path: str | Path) -> None:
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    temp = target.with_suffix(target.suffix + ".tmp")
    temp.write_text(json.dumps(calibration, indent=2, sort_keys=True), encoding="utf-8")
    temp.replace(target)


def load_calibration(path: str | Path) -> Optional[dict]:
    target = Path(path)
    if not target.exists():
        return None
    try:
        payload = json.loads(target.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return None
    if not isinstance(payload, dict) or payload.get("version") != CALIBRATION_VERSION:
        return None
    if not isinstance(payload.get("buckets"), list):
        return None
    return payload


def calibration_index(calibration: Mapping[str, object]) -> Dict[Tuple[str, str], dict]:
    index: Dict[Tuple[str, str], dict] = {}
    for row in calibration.get("buckets", []):
        if isinstance(row, dict) and row.get("level") and row.get("key"):
            index[(str(row["level"]), str(row["key"]))] = row
    return index


def estimate_candidate(
    candidate: EarlySwingCandidate,
    calibration: Optional[Mapping[str, object]],
    config: EarlySwingConfig = DEFAULT_EARLY_SWING_CONFIG,
) -> CalibrationEstimate:
    if not calibration:
        return CalibrationEstimate()
    index = calibration_index(calibration)
    for level, key in candidate_bucket_keys(candidate, config):
        row = index.get((level, key))
        if not row:
            continue
        sample_size = int(row.get("sample_size") or 0)
        if sample_size < config.calibration_minimum_sample:
            continue
        return CalibrationEstimate(
            probability_label="EMPIRICAL_HIT_RATE",
            hit_rate_pct=float(row.get("hit_rate_pct")) if row.get("hit_rate_pct") is not None else None,
            sample_size=sample_size,
            confidence=sample_confidence(sample_size, config),
            ci_low_pct=float(row.get("ci_low_pct")) if row.get("ci_low_pct") is not None else None,
            ci_high_pct=float(row.get("ci_high_pct")) if row.get("ci_high_pct") is not None else None,
            calibration_key=key,
            fallback_level=level,
        )
    return CalibrationEstimate()


def apply_calibration(
    candidates: Sequence[EarlySwingCandidate],
    calibration: Optional[Mapping[str, object]],
    config: EarlySwingConfig = DEFAULT_EARLY_SWING_CONFIG,
) -> List[EarlySwingCandidate]:
    for candidate in candidates:
        if candidate.rejected:
            continue
        estimate = estimate_candidate(candidate, calibration, config)
        candidate.empirical_probability_label = estimate.probability_label
        candidate.empirical_hit_rate_pct = estimate.hit_rate_pct
        candidate.empirical_sample_size = estimate.sample_size
        candidate.empirical_confidence = estimate.confidence
        candidate.empirical_ci_low_pct = estimate.ci_low_pct
        candidate.empirical_ci_high_pct = estimate.ci_high_pct
        candidate.calibration_key = estimate.calibration_key
        candidate.calibration_fallback_level = estimate.fallback_level
    return list(candidates)


def calibration_summary_frame(calibration: Optional[Mapping[str, object]]) -> pd.DataFrame:
    if not calibration:
        return pd.DataFrame(columns=[
            "level", "key", "sample_size", "hit_rate_pct", "ci_low_pct", "ci_high_pct",
            "average_return_pct", "average_mfe_pct", "average_mae_pct", "false_breakout_rate_pct",
        ])
    frame = pd.DataFrame(calibration.get("buckets", []))
    if frame.empty:
        return frame
    return frame.sort_values(["level", "sample_size"], ascending=[True, False]).reset_index(drop=True)
