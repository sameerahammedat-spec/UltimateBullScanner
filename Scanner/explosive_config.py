"""
Central configuration for the Next-Day Explosive Move Engine.

Every threshold used anywhere in the pattern/score/calibration/label code
lives here, as a single documented dataclass - nothing else in the engine
should contain an unexplained magic number. Tune these here, not scattered
across pattern_engine/score/label files.
"""

from dataclasses import dataclass, field


@dataclass
class ExplosiveConfig:
    # --- Data requirements (Section 3) ---
    min_history_preferred: int = 250      # preferred analysis history, trading sessions
    min_history_acceptable: int = 120     # below this -> INSUFFICIENT_DATA
    calibration_years: int = 3            # how much history to fetch for Phase-1 empirical calibration
    stale_data_max_days: int = 5          # most recent bar older than this (calendar days) -> STALE flag

    # --- Liquidity filters (Section 15) - override per-universe via with_liquidity_profile() ---
    min_median_traded_value_cr: float = 0.5   # median 20d traded value (Close x Volume), in Rs Cr
    min_median_volume: float = 50_000
    max_zero_volume_pct: float = 5.0          # % of sessions with zero volume before rejecting
    min_price: float = 2.0                    # penny-stock floor

    # --- Pattern tolerances (Section 5) ---
    swing_order: int = 3                      # bars each side for a fractal swing pivot
    resistance_touch_tol_atr_mult: float = 0.5  # two highs count as "the same resistance" within this many ATRs
    vcp_lookback: int = 40
    vcp_min_swings: int = 2
    tight_base_min_days: int = 5
    tight_base_max_days: int = 25
    tight_base_max_depth_pct: float = 15.0    # max peak-to-trough depth within the base
    ascending_triangle_lookback: int = 60
    bull_flag_max_retracement_pct: float = 50.0  # retracement of the pole must stay below this
    bull_flag_max_flag_range_pct: float = 8.0

    # --- Pivot / breakout tolerances (Section 6-7, 17) ---
    pivot_proximity_pct: float = 3.0          # "near pivot" = within this % below it
    pivot_extension_max_pct: float = 8.0      # price already this far ABOVE pivot -> extended, not breakout-ready
    breakout_trigger_buffer_pct: float = 0.3  # trigger = pivot * (1 + this/100)
    invalidation_atr_mult: float = 1.5
    max_risk_to_invalidation_pct: float = 8.0 # reject if risk to invalidation exceeds this
    min_reward_risk_ratio: float = 1.5        # available upside to resistance / risk to invalidation

    # --- Extension / exhaustion penalties (Section 9-10) ---
    ema20_extension_penalty_start_pct: float = 12.0   # % above EMA20 where extension penalty starts
    ema20_extension_penalty_max_pct: float = 25.0     # % above EMA20 where extension penalty is maxed
    extension_penalty_max: float = 25.0
    upper_wick_penalty_pct_threshold: float = 60.0    # upper wick % of range above this -> penalty
    upper_wick_penalty_max: float = 15.0
    distribution_vol_ratio_threshold: float = 1.5     # down-day volume vs up-day volume ratio
    distribution_penalty_max: float = 15.0
    failed_breakout_penalty: float = 20.0
    weak_market_penalty_max: float = 15.0
    illiquidity_penalty: float = 20.0                 # applied (not hard-reject) for borderline cases

    # --- Volume / accumulation thresholds (Section 6) ---
    volume_dry_up_ratio_threshold: float = 0.7   # 5d median vol / 20d median vol below this = dry-up
    breakout_volume_ratio_threshold: float = 1.5 # today's vol / 20d median vol above this = confirming

    # --- Score component weights (Section 8) - must sum to 100 ---
    weight_pattern_quality: float = 25.0
    weight_compression: float = 15.0
    weight_trend_rs: float = 20.0
    weight_volume: float = 15.0
    weight_pivot_candle: float = 10.0
    weight_market_regime: float = 10.0
    weight_liquidity_data_quality: float = 5.0

    # --- Historical outcome label (Section 11) ---
    min_next_day_high_return_pct: float = 4.0
    next_day_atr_multiplier: float = 1.0       # label threshold = max(min_pct, ATR% * this multiplier)
    min_next_day_close_return_pct: float = 1.0
    min_next_day_volume_ratio: float = 1.2
    max_adverse_excursion_pct: float = 3.0
    # separate thresholds for small/micro caps (typically more volatile -> higher bar)
    smallcap_min_next_day_high_return_pct: float = 6.0
    smallcap_mkt_cap_cr_threshold: float = 5000.0  # below this market cap uses smallcap thresholds

    # --- Calibration (Section 13) ---
    min_calibration_sample_size: int = 15   # below this -> NOT_AVAILABLE, never show a probability

    # --- Final classification bands (Section 18) - configurable, validate before trusting ---
    band_a_plus_min: float = 90.0
    band_a_min: float = 82.0
    band_b_plus_min: float = 75.0
    band_b_min: float = 68.0
    # below band_b_min -> excluded from the primary Explosive Watchlist entirely

    # --- Performance / run modes (Section 22) ---
    max_workers: int = 8
    top_n_report_limit: int = 30
    cache_duration_hours: int = 6

    def with_liquidity_profile(self, profile: str) -> "ExplosiveConfig":
        """Returns a COPY of this config with liquidity defaults adjusted for
        a specific universe. Different scanners see very different typical
        liquidity - a threshold sane for Nifty 500 would reject almost the
        entire BSE broad universe, and a threshold loose enough for BSE
        micro-caps would let illiquid junk into the NSE scan."""
        import copy
        cfg = copy.deepcopy(self)
        profile = profile.lower()
        if profile == "nse500":
            cfg.min_median_traded_value_cr = 1.0
            cfg.min_median_volume = 50_000
        elif profile == "bse_gainers":
            cfg.min_median_traded_value_cr = 0.3
            cfg.min_median_volume = 20_000
        elif profile == "bse_broad":
            cfg.min_median_traded_value_cr = 0.15
            cfg.min_median_volume = 10_000
        else:
            raise ValueError(f"Unknown liquidity profile: {profile!r}. "
                              f"Expected one of: nse500, bse_gainers, bse_broad")
        return cfg

    def validate_weights(self) -> None:
        total = (self.weight_pattern_quality + self.weight_compression + self.weight_trend_rs +
                 self.weight_volume + self.weight_pivot_candle + self.weight_market_regime +
                 self.weight_liquidity_data_quality)
        if abs(total - 100.0) > 0.01:
            raise ValueError(f"Score component weights must sum to 100, got {total}")


DEFAULT_CONFIG = ExplosiveConfig()
DEFAULT_CONFIG.validate_weights()
