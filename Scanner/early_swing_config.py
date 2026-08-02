"""Central configuration for the Early Swing Rally Scanner."""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict, Tuple


@dataclass(frozen=True)
class EarlySwingConfig:
    # Data and runtime
    history_period: str = "1y"
    minimum_history_sessions: int = 120
    preferred_history_sessions: int = 220
    stale_calendar_days: int = 7
    batch_size: int = 80
    download_workers: int = 8
    fetch_retries: int = 2
    fetch_timeout_seconds: int = 25
    batch_pause_seconds: float = 0.50
    cache_hours: int = 8
    cache_directory: str = ".early_swing_cache"
    benchmark_ticker: str = "^BSESN"

    # Universe / liquidity
    minimum_price: float = 20.0
    minimum_median_traded_value_cr: float = 1.0
    maximum_zero_volume_pct: float = 5.0

    # Setup geometry
    rally_age_max_sessions: int = 2
    breakout_lookbacks: Tuple[int, ...] = (10, 15, 20, 25, 30)
    breakout_buffer_pct: float = 0.15
    breakout_min_return_pct: float = 1.20
    breakout_min_volume_ratio: float = 1.35
    followthrough_min_volume_ratio: float = 0.60
    minimum_close_location_value: float = 0.55
    maximum_base_depth_pct: float = 18.0
    pivot_touch_tolerance_pct: float = 1.25
    minimum_pivot_touches: int = 2

    # Momentum / extension controls
    preferred_rsi_range: Tuple[float, float] = (55.0, 72.0)
    hard_maximum_rsi: float = 82.0
    minimum_adx: float = 15.0
    maximum_above_ema20_pct: float = 8.0
    maximum_three_day_return_pct: float = 15.0
    maximum_five_day_return_pct: float = 22.0
    maximum_ten_day_return_pct: float = 32.0
    maximum_consecutive_expansion_candles: int = 3

    # False-breakout controls
    maximum_upper_wick_pct: float = 45.0
    maximum_false_breakout_risk: float = 65.0
    maximum_extension_risk: float = 75.0

    # Trade construction
    entry_buffer_pct: float = 0.15
    maximum_chase_above_pivot_pct: float = 3.5
    atr_stop_multiple: float = 1.20
    stop_buffer_atr: float = 0.10
    maximum_stop_distance_pct: float = 7.0
    minimum_reward_risk: float = 1.80
    minimum_distance_to_resistance_pct: float = 4.0
    target1_r_multiple: float = 2.0
    target2_r_multiple: float = 3.0
    account_size: float = 100_000.0
    account_risk_pct: float = 1.0

    # Ranking / output
    exceptional_score: float = 85.0
    high_quality_score: float = 75.0
    watchlist_score: float = 65.0
    maximum_actionable_results: int = 10

    # Backtest defaults
    backtest_holding_sessions: int = 10
    backtest_target_pct: float = 6.0
    backtest_slippage_pct: float = 0.15
    backtest_cost_pct: float = 0.20

    # Phase 2: walk-forward backtest and pooled empirical calibration
    backtest_holding_windows: Tuple[int, ...] = (3, 5, 7, 10, 15)
    backtest_entry_methods: Tuple[str, ...] = ("NEXT_OPEN", "ABOVE_SIGNAL_HIGH")
    backtest_target_methods: Tuple[str, ...] = ("FIXED_PERCENT", "CANDIDATE_TARGET1")
    backtest_minimum_gap_sessions: int = 3
    calibration_minimum_sample: int = 30
    calibration_medium_sample: int = 100
    calibration_high_sample: int = 300
    calibration_file: str = "early_swing_calibration.json"
    calibration_trades_file: str = "early_swing_backtest_trades.csv"
    calibration_summary_file: str = "early_swing_backtest_summary.csv"
    calibration_history_period: str = "5y"
    calibration_cache_hours: int = 168
    calibration_score_bands: Tuple[Tuple[float, float, str], ...] = (
        (85.0, 100.0, "85-100"),
        (75.0, 84.9999, "75-84"),
        (65.0, 74.9999, "65-74"),
        (0.0, 64.9999, "0-64"),
    )

    # Phase 3: persistent alert generation and duplicate suppression
    alert_enabled: bool = True
    alert_minimum_rules_score: float = 75.0
    alert_minimum_empirical_hit_rate_pct: float = 0.0
    alert_minimum_empirical_sample: int = 0
    alert_cooldown_days: int = 5
    alert_setup_expiry_days: int = 15
    alert_state_file: str = "early_swing_alert_state.json"
    alert_output_file: str = "early_swing_alerts.csv"
    alert_allow_watch_status: bool = False
    alert_price_tolerance_pct: float = 0.50

    # Scoring component maxima. Delivery is optional and therefore excluded from
    # the live maximum when unavailable; the score is normalised to 100.
    score_weights: Dict[str, float] = field(default_factory=lambda: {
        "setup": 22.0,
        "volume": 15.0,
        "trend_momentum": 15.0,
        "relative_strength_market": 12.0,
        "remaining_upside": 14.0,
        "trade_structure": 12.0,
        "data_liquidity": 5.0,
        "delivery": 5.0,
    })


DEFAULT_EARLY_SWING_CONFIG = EarlySwingConfig()
