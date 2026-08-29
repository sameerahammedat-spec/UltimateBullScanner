"""
Synthetic integration test for today's-results -> afternoon fresh-move pipeline.

Run from the Scanner directory:
    python test_results_integration_synthetic.py

This test does NOT call Yahoo, BSE, news, SMS, voice, WhatsApp or NTFY.
It exercises the REAL AfternoonScanner.broad_scan()/deep_scan() flow while
replacing only external market/result data with deterministic synthetic data.
"""
from __future__ import annotations
from dataclasses import replace

import copy
from dataclasses import dataclass
from datetime import datetime
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import pandas as pd

import afternoon_fresh_move_scanner as mod
from afternoon_config import DEFAULT_AFTERNOON_CONFIG

TZ = ZoneInfo("Asia/Kolkata")
NOW = datetime(2026, 8, 24, 14, 45, tzinfo=TZ)
SYNTH_ID = "SYNTHRESULT"
SYNTH_CODE = "999999"
SYNTH_TICKER = "SYNTHRESULT.BO"

STOCK = {
    "security_code": SYNTH_CODE,
    "security_id": SYNTH_ID,
    "name": "Synthetic Results Test Ltd",
    "group": "A",
    "isin": "SYNTHISIN",
    "is_smallcap": True,
    "ticker": SYNTH_TICKER,
    "alternate_tickers": [],
    "stage0_source": "SYNTHETIC_RESULTS_TEST",
}


@dataclass
class SyntheticResultEvent:
    result_date: str = "2026-08-24"
    board_meeting_date: str = "2026-08-24"
    expected_time: str = "14:30"
    source: str = "SYNTHETIC_RESULTS_TEST"
    result_type: str = "QUARTERLY_RESULTS"
    release_confirmed: bool = True
    release_time: str = "14:30"


EVENT = SyntheticResultEvent()


def make_intraday():
    idx = pd.date_range(
        "2026-08-24 13:30",
        "2026-08-24 14:45",
        freq="5min",
        tz=TZ,
    )

    # 16 candles: 13:30 through 14:45 inclusive.
    close = [
        99.80, 100.00, 100.10, 100.00,
        100.20, 100.30, 100.25,
        102.00, 103.50, 105.00,
        106.50, 108.00, 108.20,
        108.30, 108.40, 108.20,
    ]

    volume = [
        1000, 1050, 980, 1020,
        1100, 1050, 1080,
        12000, 15000, 18000,
        21000, 24000, 26000,
        28000, 29000, 30000,
    ]

    c = pd.Series(close, index=idx, dtype=float)

    return pd.DataFrame(
        {
            "Open": c - 0.20,
            "High": c + 0.30,
            "Low": c - 0.30,
            "Close": c,
            "Adj Close": c,
            "Volume": volume,
        },
        index=idx,
    )


def make_daily():
    idx = pd.date_range("2026-07-01", "2026-08-21", freq="B", tz=TZ)
    c = pd.Series([90 + i * 0.30 for i in range(len(idx))], index=idx, dtype=float)
    return pd.DataFrame({
        "Open": c - 0.4, "High": c + 1.0, "Low": c - 1.0,
        "Close": c, "Adj Close": c, "Volume": [100000] * len(idx),
    }, index=idx)


INTRADAY = make_intraday()
DAILY = make_daily()


class FakePrefilter:
    qualified = True
    latest_price = 108.20
    window_move_pct = 7.90
    pre_window_move_pct = 0.20
    ret_15m_pct = 2.90
    ret_30m_pct = 7.60
    window_turnover_cr = 5.40
    volume_acceleration = 10.8
    distance_from_window_high_pct = 0.0
    day_high = 108.40
    day_low = 99.60

    def to_dict(self):
        return {
            "latest_price": self.latest_price,
            "window_move_pct": self.window_move_pct,
            "pre_window_move_pct": self.pre_window_move_pct,
            "ret_15m_pct": self.ret_15m_pct,
            "ret_30m_pct": self.ret_30m_pct,
            "window_turnover_cr": self.window_turnover_cr,
            "volume_acceleration": self.volume_acceleration,
            "distance_from_window_high_pct": self.distance_from_window_high_pct,
            "day_high": self.day_high,
            "day_low": self.day_low,
        }


class FakeDailyFeatures:
    rsi14 = 67.0
    atr_pct = 3.2
    prior_2d_return_pct = 4.5
    prior_5d_return_pct = 8.5
    breakout_20d_pct = 3.8
    room_to_resistance_pct = 12.0

    def to_dict(self):
        return {
            "atr_pct": self.atr_pct,
            "prior_2d_return_pct": self.prior_2d_return_pct,
            "prior_5d_return_pct": self.prior_5d_return_pct,
            "breakout_20d_pct": self.breakout_20d_pct,
            "room_to_resistance_pct": self.room_to_resistance_pct,
        }


class FakeContinuation:
    sample_size = 42
    hit_5_pct = 0.74
    hit_8_pct = 0.57
    hit_10_pct = 0.43
    hit_15_pct = 0.26
    hit_20_pct = 0.14
    p75_mfe_pct = 13.5


class SyntheticResultsMonitor:
    watchlist_called = False
    event_called = False

    def __init__(self, universe, *args, **kwargs):
        self.universe = universe

    def watchlist(self, now):
        type(self).watchlist_called = True
        return [dict(STOCK)]

    def event_for_stock(self, stock, now):
        if str(stock.get("security_id", "")).upper() == SYNTH_ID:
            type(self).event_called = True
            return EVENT
        return None


def fake_prefilter(*args, **kwargs):
    return FakePrefilter()


def fake_rvol(*args, **kwargs):
    return 13.5


def fake_intraday_rsi(*args, **kwargs):
    return 72.0


def fake_daily_features(*args, **kwargs):
    return FakeDailyFeatures()


def fake_continuation(*args, **kwargs):
    return FakeContinuation()


def fake_score(*args, **kwargs):
    return {
        "score": 78.0,
        "momentum_score": 82.0,
        "swing_score": 76.0,
        "score_breakdown": "Synthetic strong post-result momentum",
        "rejected": False,
        "reasons": ["Fresh post-2PM move", "Strong same-window volume"],
        "warnings": [],
        "hard_rejects": [],
    }


def fake_risk(*args, **kwargs):
    return {
        "entry": 108.20,
        "stop_loss": 104.80,
        "risk_pct": 3.14,
        "target1": 115.00,
        "target2": 119.00,
        "rr_target1": 2.0,
        "rr_target2": 3.18,
    }


def fake_catalyst(*args, **kwargs):
    return SimpleNamespace(score=0.0, strength="NONE", reason="Synthetic test", headlines=[], warnings="")


def check(condition, message):
    if not condition:
        raise AssertionError("FAIL: " + message)
    print("[PASS] " + message)


def main():
    print("=" * 78)
    print(" TODAY'S RESULTS -> AFTERNOON FRESH-MOVE SYNTHETIC INTEGRATION TEST")
    print("=" * 78)
    print("Simulated time :", NOW.strftime("%Y-%m-%d %H:%M:%S %Z"))
    print("Result stock   :", SYNTH_ID, "/", SYNTH_CODE, "/", SYNTH_TICKER)
    print("Result release : 14:30 IST (confirmed)")
    print("Post-2PM move  : +7.9%")
    print("Same-window RVOL: 13.5x")
    print()

    names = [
        "ResultsMonitor", "batch_download", "compute_intraday_prefilter",
        "compute_same_window_rvol", "compute_intraday_rsi",
        "compute_daily_features", "calibrate_historical_continuation",
        "score_candidate", "build_risk_levels", "assess_catalyst",
    ]
    originals = {name: getattr(mod, name) for name in names}

    try:
        mod.ResultsMonitor = SyntheticResultsMonitor
        mod.compute_intraday_prefilter = fake_prefilter
        mod.compute_same_window_rvol = fake_rvol
        mod.compute_intraday_rsi = fake_intraday_rsi
        mod.compute_daily_features = fake_daily_features
        mod.calibrate_historical_continuation = fake_continuation
        mod.score_candidate = fake_score
        mod.build_risk_levels = fake_risk
        mod.assess_catalyst = fake_catalyst

        def fake_batch_download(tickers, period, interval, batch_size, auto_adjust=False):
            out = {}
            for ticker in tickers:
                if ticker == SYNTH_TICKER:
                    out[ticker] = INTRADAY.copy() if interval == "5m" else DAILY.copy()
                elif ticker == "^BSESN":
                    out[ticker] = INTRADAY.copy()
            return out

        mod.batch_download = fake_batch_download

        # Do not mutate the production DEFAULT config object.
        cfg = replace(
            DEFAULT_AFTERNOON_CONFIG,
            full_universe_scan=False,
            stage0_bse_enabled=False,
            deep_candidate_limit=10,
            catalyst_candidate_limit=0,
            broad_batch_size=10,
        )

        scanner = mod.AfternoonScanner(
            universe=[STOCK],
            output_dir="afternoon_reports_synthetic_results_test",
            config=cfg,
        )

        watch = scanner.results_monitor.watchlist(NOW)
        check(SyntheticResultsMonitor.watchlist_called, "ResultsMonitor.watchlist() was called")
        check(len(watch) == 1 and watch[0]["security_id"] == SYNTH_ID,
              "Today's synthetic result stock is present in the Results watchlist")

        broad = scanner.broad_scan(NOW)
        check(any(x.get("security_id") == SYNTH_ID for x in broad),
              "Today's result stock was forced into the afternoon verification universe")

        reportable, rejected = scanner.deep_scan(broad, NOW)
        all_items = reportable + rejected
        check(SyntheticResultsMonitor.event_called, "event_for_stock() was called during deep scan")
        check(all_items, "Synthetic post-result stock reached deep-scan output")

        item = next(x for x in all_items if x.get("security_id") == SYNTH_ID)
        check(item.get("results_scheduled_today") is True, "results_scheduled_today=True")
        check(item.get("results_release_confirmed") is True, "results_release_confirmed=True")
        check(item.get("results_result_date") == "2026-08-24", "result date propagated")
        check(item.get("results_expected_time") == "14:30", "result expected time propagated")
        check(item.get("results_release_time") == "14:30", "result release time propagated")
        check(item.get("results_source") == "SYNTHETIC_RESULTS_TEST", "result source propagated")
        check(item.get("results_type") == "QUARTERLY_RESULTS", "result type propagated")
        check(item.get("results_priority") == "A_RESULTS_TODAY", "Group-A results priority assigned")
        check(item.get("score_before_results_overlay") is not None,
              "Base score captured before results overlay")
        check(item.get("results_priority_bonus") is not None,
              "Results priority bonus generated")
        check(item.get("results_setup_score") is not None,
              "Results setup score generated")
        check(float(item.get("window_move_pct") or 0) >= 5.0,
              "Strong fresh post-2PM move reached the integrated candidate")
        check(float(item.get("same_window_rvol") or 0) >= 10.0,
              "Strong same-window RVOL reached the integrated candidate")

        print()
        print("-" * 78)
        print("SYNTHETIC INTEGRATION RESULT")
        print("-" * 78)
        for key in (
            "security_id", "name", "results_scheduled_today",
            "results_release_confirmed", "results_result_date",
            "results_expected_time", "results_release_time", "results_source",
            "results_type", "results_priority", "window_move_pct",
            "same_window_rvol", "score_before_results_overlay",
            "results_priority_bonus", "extension_penalty", "results_setup_score",
            "score", "status",
        ):
            print(f"{key:32}: {item.get(key)}")

        print()
        print("=" * 78)
        print("RESULT: PASS")
        print("The today's-results integration is connected to the afternoon scanner.")
        print("No live market/API/notification service was used.")
        print("=" * 78)

    finally:
        for name, value in originals.items():
            setattr(mod, name, value)


if __name__ == "__main__":
    main()
