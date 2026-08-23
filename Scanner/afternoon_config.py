"""Configuration for the 2:00 PM-3:10 PM fresh-move scanner.

All thresholds are deliberately centralized here so the live scanner can be
backtested/tuned without scattering magic numbers through the implementation.
The defaults are conservative: they favour fresh, liquid, confirmed moves and
reject stocks that were already extended before 2 PM.
"""
from dataclasses import dataclass


@dataclass(frozen=True)
class AfternoonConfig:
    timezone: str = "Asia/Kolkata"
    observation_start: str = "13:30"
    window_start: str = "14:00"
    window_end: str = "15:10"
    # Production coverage guarantee: bypass BSE stage-0 breadth reduction.
    # This makes every watch cycle verify the complete loaded universe.
    full_universe_scan: bool = True
    scan_interval_minutes: int = 5

    # Stage-0 breadth reduction. BSE's website market-movers endpoint is used
    # only as a best-effort shortlist; the actual 2PM move is always verified
    # from 5-minute bars before a stock can qualify.
    stage0_bse_enabled: bool = True
    stage0_each_order_limit: int = 250
    stage0_min_day_gain_pct: float = 0.25
    fallback_group_b_shards: int = 5

    # Broad-universe 5m prefilter.
    broad_batch_size: int = 120
    min_window_move_pct: float = 2.0
    ideal_window_move_pct: float = 3.0
    max_window_move_pct: float = 8.5
    max_pre_window_move_pct: float = 2.75
    min_window_turnover_cr: float = 0.60
    min_intraday_volume_acceleration: float = 1.35
    min_bars_in_window: int = 2
    max_distance_from_window_high_pct: float = 1.40
    min_positive_bar_fraction: float = 0.55

    # Deep validation.
    deep_candidate_limit: int = 45
    catalyst_candidate_limit: int = 12
    min_same_window_rvol: float = 1.75
    strong_same_window_rvol: float = 2.50
    same_window_lookback_days: int = 10
    daily_history_period: str = "2y"
    intraday_history_period: str = "15d"
    max_prior_5d_return_pct: float = 15.0
    max_prior_2d_return_pct: float = 10.0
    max_atr_pct: float = 9.0
    min_atr_pct: float = 1.25
    min_room_to_resistance_pct: float = 4.0
    hard_min_room_to_resistance_pct: float = 2.25
    max_entry_risk_pct: float = 4.5
    max_latest_upper_wick_pct: float = 45.0
    max_intraday_rsi: float = 86.0

    # Scoring / alerts.
    min_report_score: float = 65.0
    sms_alert_score: float = 76.0
    # Phone + SMS/WhatsApp should fire for every actionable immediate-review setup.
    # Keep the display label for 84+ separate via high_confidence_score.
    call_alert_score: float = 76.0
    high_confidence_score: float = 84.0
    alert_cooldown_minutes: int = 20
    require_catalyst_for_call: bool = False

    # Historical continuation profile. These are diagnostics, not promises.
    calibration_min_samples: int = 8
    calibration_event_min_gain_pct: float = 2.0
    calibration_event_max_gain_pct: float = 9.0
    calibration_min_volume_ratio: float = 1.4
    calibration_forward_days: int = 3

    # Risk/target model.
    stop_atr_multiple: float = 1.15
    breakout_stop_buffer_pct: float = 1.25
    target1_r_multiple: float = 2.0
    target2_r_multiple: float = 3.0

    # Network/catalyst behaviour.
    request_timeout_seconds: int = 12
    bse_announcement_max_pages: int = 8
    google_news_max_headlines: int = 6


DEFAULT_AFTERNOON_CONFIG = AfternoonConfig()
