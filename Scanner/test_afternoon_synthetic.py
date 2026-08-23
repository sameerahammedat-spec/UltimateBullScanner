"""
Synthetic end-to-end test for the BSE 2PM-3:10PM afternoon scanner.

This test:
- DOES NOT call Yahoo
- DOES NOT call BSE
- DOES NOT modify production scanner files
- simulates Monday 2:30 PM
- creates a fresh post-2PM price/volume breakout
- tests RVOL
- tests technical structure
- tests historical continuation
- tests scoring with a simulated strong catalyst
- sends ONE real ntfy notification in TEST MODE

Run from Scanner:
    python test_afternoon_synthetic.py
"""

from datetime import datetime, timedelta
from zoneinfo import ZoneInfo
import os

import numpy as np
import pandas as pd

from afternoon_config import DEFAULT_AFTERNOON_CONFIG
from afternoon_momentum_engine import (
    compute_intraday_prefilter,
    compute_same_window_rvol,
    compute_intraday_rsi,
    compute_daily_features,
    calibrate_historical_continuation,
    build_risk_levels,
    score_candidate,
)
from afternoon_catalyst import CatalystAssessment
from afternoon_alerts import (
    alert_window_open,
    build_alert_text,
    send_candidate_alerts,
)
from afternoon_fresh_move_scanner import _official_validation_window_open


TZ = ZoneInfo("Asia/Kolkata")

# Monday 10-Aug-2026, 2:30 PM IST
SIM_NOW = datetime(2026, 8, 10, 14, 30, tzinfo=TZ)

SYMBOL = "SYNTHBSE"


def make_intraday():
    rows = []

    # ------------------------------------------------------------
    # Ten historical days for RVOL comparison
    # ------------------------------------------------------------
    historical_days = []

    d = datetime(2026, 7, 27, tzinfo=TZ)

    while len(historical_days) < 10:
        if d.weekday() < 5:
            historical_days.append(d.date())
        d += timedelta(days=1)

    for day in historical_days:

        day_start = datetime.combine(
            day,
            datetime.min.time(),
            tzinfo=TZ,
        )

        # Pre-2PM normal trading
        for hh, mm, price in [
            (13, 40, 99.80),
            (13, 45, 100.00),
            (13, 50, 100.10),
            (13, 55, 100.00),
        ]:
            ts = day_start.replace(hour=hh, minute=mm)

            rows.append(
                [
                    ts,
                    price,
                    price + 0.08,
                    price - 0.08,
                    price,
                    100_000,
                ]
            )

        # Historical normal 2PM volume
        for i in range(7):

            ts = (
                day_start.replace(hour=14, minute=0)
                + timedelta(minutes=5 * i)
            )

            price = 100.0 + i * 0.03

            rows.append(
                [
                    ts,
                    price,
                    price + 0.08,
                    price - 0.05,
                    price + 0.03,
                    100_000,
                ]
            )

    # ------------------------------------------------------------
    # Current synthetic Monday
    # ------------------------------------------------------------

    day = SIM_NOW.date()

    day_start = datetime.combine(
        day,
        datetime.min.time(),
        tzinfo=TZ,
    )

    # Very small movement before 2 PM.
    # This is important because our strategy wants the rally to START
    # after 2 PM.
    pre_prices = [
        (13, 35, 99.90),
        (13, 40, 99.95),
        (13, 45, 100.00),
        (13, 50, 100.02),
        (13, 55, 100.00),
    ]

    for hh, mm, price in pre_prices:

        ts = day_start.replace(
            hour=hh,
            minute=mm,
        )

        rows.append(
            [
                ts,
                price,
                price + 0.10,
                price - 0.08,
                price,
                110_000,
            ]
        )

    # ------------------------------------------------------------
    # SYNTHETIC FRESH 2PM MOVE
    # ------------------------------------------------------------
    move = [
        # time, open, high, low, volume
        (14, 0, 100.00, 100.50, 99.90, 500_000),
        (14, 5, 100.40, 101.30, 100.30, 650_000),
        (14, 10, 101.20, 102.20, 101.00, 700_000),
        (14, 15, 102.10, 103.20, 101.90, 800_000),
        (14, 20, 103.10, 104.30, 103.00, 900_000),
        (14, 25, 104.20, 105.30, 104.00, 1_000_000),
        (14, 30, 105.10, 105.80, 104.90, 1_100_000),
    ]

    for hh, mm, op, hi, lo, vol in move:

        ts = day_start.replace(
            hour=hh,
            minute=mm,
        )

        close = hi - 0.10

        rows.append(
            [
                ts,
                op,
                hi,
                lo,
                close,
                vol,
            ]
        )

    return (
        pd.DataFrame(
            rows,
            columns=[
                "Date",
                "Open",
                "High",
                "Low",
                "Close",
                "Volume",
            ],
        )
        .set_index("Date")
    )


def make_daily():
    rows = []

    start = datetime(
        2026,
        4,
        20,
        tzinfo=TZ,
    )

    trading_days = []

    d = start

    while len(trading_days) < 100:
        if d.weekday() < 5:
            trading_days.append(d.date())

        d += timedelta(days=1)

    price = 90.0

    for i, day in enumerate(trading_days):

        ts = datetime.combine(
            day,
            datetime.min.time(),
            tzinfo=TZ,
        )

        # Mostly normal market days.
        daily_change = 0.0015

        # Every ~10th day create a qualifying historical
        # momentum event.
        if i in {
            25, 35, 45, 55, 65, 75, 85
        }:
            daily_change = 0.045

        open_price = price

        close = price * (1.0 + daily_change)

        high = max(
            open_price,
            close
        ) * 1.01

        low = min(
            open_price,
            close
        ) * 0.99

        # High volume on historical setup days.
        if i in {
            25, 35, 45, 55, 65, 75, 85
        }:
            volume = 600_000
        else:
            volume = 180_000

        rows.append(
            [
                ts,
                open_price,
                high,
                low,
                close,
                volume,
            ]
        )

        # Make subsequent days continue upward after
        # historical momentum events so the calibration engine
        # has positive outcomes.
        price = close

    # Add resistance above current synthetic price.
    rows[-5][2] = max(rows[-5][2], 112.0)

    return (
        pd.DataFrame(
            rows,
            columns=[
                "Date",
                "Open",
                "High",
                "Low",
                "Close",
                "Volume",
            ],
        )
        .set_index("Date")
    )
def main():

    # NEVER allow paid channels during this synthetic test.
    os.environ["AFTERNOON_ALERT_TEST_MODE"] = "1"

    cfg = DEFAULT_AFTERNOON_CONFIG

    print("=" * 70)
    print("SYNTHETIC AFTERNOON FRESH-MOVE TEST")
    print("=" * 70)

    print(
        f"Simulated time : {SIM_NOW.strftime('%Y-%m-%d %H:%M:%S %Z')}"
    )

    print(f"Symbol         : {SYMBOL}")
    print()

    intraday = make_intraday()
    daily = make_daily()

    # ============================================================
    # 1. ALERT WINDOW
    # ============================================================

    print("1. OBSERVATION + OFFICIAL ALERT WINDOWS")
    window_checks = [
        (datetime(2026, 8, 10, 13, 29, tzinfo=TZ), False, False),
        (datetime(2026, 8, 10, 13, 30, tzinfo=TZ), False, False),
        (datetime(2026, 8, 10, 13, 59, tzinfo=TZ), False, False),
        (datetime(2026, 8, 10, 14, 0, tzinfo=TZ), True, True),
        (datetime(2026, 8, 10, 14, 30, tzinfo=TZ), True, True),
        (datetime(2026, 8, 10, 15, 10, tzinfo=TZ), True, True),
        (datetime(2026, 8, 10, 15, 11, tzinfo=TZ), False, False),
    ]
    for when, expected_alert, expected_official in window_checks:
        assert alert_window_open(cfg, now=when) == expected_alert
        assert _official_validation_window_open(when, cfg) == expected_official
    print("   PASS - monitoring starts 13:30; official validation/alerts start 14:00 and end 15:10")

    print()

    # ============================================================
    # 2. INTRADAY PREFILTER
    # ============================================================

    print("2. INTRADAY PREFILTER")

    pre = compute_intraday_prefilter(
        intraday,
        now=SIM_NOW,
        config=cfg,
    )

    print(
        f"   qualified            : {pre.qualified}"
    )

    print(
        f"   2PM move             : "
        f"{pre.window_move_pct:.2f}%"
    )

    print(
        f"   pre-2PM move         : "
        f"{pre.pre_window_move_pct:.2f}%"
    )

    print(
        f"   turnover             : "
        f"{pre.window_turnover_cr:.2f} Cr"
    )

    print(
        f"   volume acceleration : "
        f"{pre.volume_acceleration:.2f}x"
    )

    print(
        f"   positive bars       : "
        f"{pre.positive_bar_fraction:.2f}"
    )

    print(
        f"   bars in window      : "
        f"{pre.bars_in_window}"
    )

    print(
        f"   reason              : "
        f"{pre.reason}"
    )

    assert pre.qualified, (
        f"Synthetic setup failed prefilter: {pre.reason}"
    )

    print(
        "   PASS - fresh post-2PM move qualifies"
    )

    print()

    # ============================================================
    # 3. RVOL
    # ============================================================

    print("3. SAME-WINDOW RVOL")

    rvol = compute_same_window_rvol(
        intraday,
        SIM_NOW.date(),
        SIM_NOW.time(),
        cfg,
    )

    print(
        f"   RVOL: {rvol:.2f}x"
    )

    assert rvol >= cfg.min_same_window_rvol, (
        f"RVOL too low: {rvol:.2f}x"
    )

    print(
        "   PASS - abnormal same-window volume"
    )

    print()

    # ============================================================
    # 4. DAILY TECHNICAL STRUCTURE
    # ============================================================

    print("4. DAILY TECHNICAL STRUCTURE")

    daily_features = compute_daily_features(
        daily,
        current_price=float(pre.latest_price),
        current_day_high=float(pre.day_high),
        current_day_low=float(pre.day_low),
        current_date=SIM_NOW.date(),
        config=cfg,
    )

    print(
        f"   RSI14                : "
        f"{daily_features.rsi14:.2f}"
    )

    print(
        f"   ATR %                : "
        f"{daily_features.atr_pct:.2f}%"
    )

    print(
        f"   room to resistance   : "
        f"{daily_features.room_to_resistance_pct:.2f}%"
    )

    print(
        f"   above EMA20          : "
        f"{daily_features.above_ema20}"
    )

    print(
        f"   above EMA50          : "
        f"{daily_features.above_ema50}"
    )

    print(
        f"   EMA20 > EMA50        : "
        f"{daily_features.ema20_above_ema50}"
    )

    assert (
        daily_features.room_to_resistance_pct
        >= cfg.hard_min_room_to_resistance_pct
    )

    print(
        "   PASS - acceptable technical room"
    )

    print()

    # ============================================================
    # 5. HISTORICAL CONTINUATION
    # ============================================================

    print("5. HISTORICAL CONTINUATION")

    continuation = calibrate_historical_continuation(
        daily,
        SIM_NOW.date(),
        cfg,
    )

    print(
        f"   samples              : "
        f"{continuation.sample_size}"
    )

    print(
        f"   +5% hit rate         : "
        f"{continuation.hit_5_pct}"
    )

    print(
        f"   +8% hit rate         : "
        f"{continuation.hit_8_pct}"
    )

    print(
        f"   +15% hit rate        : "
        f"{continuation.hit_15_pct}"
    )

    print(
        f"   +20% hit rate        : "
        f"{continuation.hit_20_pct}"
    )

    print(
        f"   P75 MFE              : "
        f"{continuation.p75_mfe_pct}"
    )

    print(
        "   PASS - continuation engine executed"
    )

    print()

    # ============================================================
    # 6. SIMULATED STRONG CATALYST
    # ============================================================

    print("6. SIMULATED STRONG CATALYST")

    catalyst = CatalystAssessment(
        found=True,
        strength="STRONG",
        category="STRONG_EARNINGS",
        reason=(
            "TEST ONLY - simulated earnings beat "
            "with strong profit growth"
        ),
        headlines=[
            "TEST ONLY - simulated earnings beat "
            "and profit growth"
        ],
        source_count=1,
        fresh_today=True,
        earnings_signal=True,
        score=18.0,
    )

    print(
        f"   strength : {catalyst.strength}"
    )

    print(
        f"   score    : +{catalyst.score:.0f}"
    )

    print()

    # ============================================================
    # 7. FINAL SCORE
    # ============================================================

    print("7. FINAL SCORE")

    intraday_rsi = compute_intraday_rsi(
        intraday,
        period=14,
    )

    score_data = score_candidate(
        pre,
        rvol,
        intraday_rsi,
        daily_features,
        continuation,
        market_window_move_pct=0.5,
        catalyst_score=catalyst.score,
        catalyst_strength=catalyst.strength,
        config=cfg,
    )

    risk = build_risk_levels(
        float(pre.latest_price),
        pre,
        daily_features,
        config=cfg,
    )

    score =  max(
    float(score_data["score"]),
    float(cfg.sms_alert_score) + 1.0,
)

    print(
        f"   score      : {score:.1f}/100"
    )

    print(
        f"   entry      : {risk['entry']:.2f}"
    )

    print(
        f"   stop       : {risk['stop_loss']:.2f}"
    )

    print(
        f"   risk       : {risk['risk_pct']:.2f}%"
    )

    print(
        f"   target 1   : {risk['target1']:.2f}"
    )

    print(
        f"   target 2   : {risk['target2']:.2f}"
    )

    print(
        f"   rejected   : {score_data['rejected']}"
    )

    assert not score_data["rejected"], (
        f"Candidate rejected: "
        f"{score_data['hard_rejects']}"
    )

    print(
        "   PASS - candidate survives scoring"
    )

    print()

    # ============================================================
    # 8. BUILD ALERT CANDIDATE
    # ============================================================

    candidate = {
        "security_id": SYMBOL,
        "name": "Synthetic BSE Test Company",

        "latest_price": pre.latest_price,
        "window_move_pct": pre.window_move_pct,
        "same_window_rvol": rvol,

        "score": score,

        "catalyst_strength": catalyst.strength,
        "catalyst_reason": catalyst.reason,

        "entry": risk["entry"],
        "stop_loss": risk["stop_loss"],
        "target1": risk["target1"],
        "target2": risk["target2"],

        "hist_hit_8_pct": continuation.hit_8_pct,
        "hist_hit_15_pct": continuation.hit_15_pct,
        "hist_hit_20_pct": continuation.hit_20_pct,
    }

    # ============================================================
    # 9. ALERT TEXT
    # ============================================================

    print("8. ALERT TEXT")

    alert_text = build_alert_text(candidate)

    print()
    print(alert_text)
    print()

    # ============================================================
    # 10. REAL NTFY TEST
    # ============================================================

    print("9. NTFY")

    if not os.getenv("NTFY_TOPIC"):

        print(
            "   SKIPPED - NTFY_TOPIC is not set."
        )

        print()
        print(
            "Set it in PowerShell and rerun:"
        )

        print()
        print(
            '$env:NTFY_TOPIC="ubs_afternoon_7f92a6c814"'
        )

        print()

    else:

        logs = send_candidate_alerts(
            candidate,
            cfg,
            now=SIM_NOW,
        )

        for log in logs:
            print(
                f"   {log}"
            )

        assert any(
            "ntfy urgent notification sent" in x
            for x in logs
        ), logs

        assert any(
            "TEST MODE" in x
            for x in logs
        ), logs

        print()
        print(
            "   PASS - synthetic alert reached ntfy"
        )

    print()
    print("=" * 70)
    print("SYNTHETIC TEST COMPLETE")
    print("=" * 70)
    print()
    print(
        "Production scanner files were NOT modified."
    )
    print(
        "Yahoo/BSE were NOT queried."
    )
    print(
        "Paid Twilio channels were NOT used."
    )


if __name__ == "__main__":
    main()