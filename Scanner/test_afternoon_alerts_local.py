"""Safe local verification for the afternoon alert layer.

Run from Scanner/ with NTFY_TOPIC set.
This sends one real ntfy test notification, then one end-to-end simulated
Monday 2:30 PM notification. Twilio/SMS/WhatsApp/email/call are suppressed by
AFTERNOON_ALERT_TEST_MODE=1.
"""
from datetime import datetime
from zoneinfo import ZoneInfo

from afternoon_alerts import (
    alert_window_open,
    build_alert_text,
    send_candidate_alerts,
    send_ntfy_alert,
)
from afternoon_config import DEFAULT_AFTERNOON_CONFIG

TZ = ZoneInfo("Asia/Kolkata")


def candidate():
    return {
        "security_id": "TESTBSE",
        "name": "Afternoon Alert Test",
        "latest_price": 250.50,
        "window_move_pct": 3.85,
        "same_window_rvol": 3.20,
        "score": 82,
        "catalyst_reason": "TEST ONLY - simulated earnings catalyst",
        "entry": 250.50,
        "stop_loss": 241.00,
        "target1": 269.50,
        "target2": 279.00,
        "hist_hit_8_pct": 67,
        "hist_hit_15_pct": 31,
        "hist_hit_20_pct": 18,
        "catalyst_strength": "STRONG",
    }


def main():
    import os

    if not os.getenv("NTFY_TOPIC"):
        raise SystemExit("ERROR: set NTFY_TOPIC before running this test")

    print("1) Time-window guard")
    checks = [
        (datetime(2026, 8, 8, 12, 30, tzinfo=TZ), False),   # Saturday
        (datetime(2026, 8, 10, 13, 59, tzinfo=TZ), False),  # before window
        (datetime(2026, 8, 10, 14, 0, tzinfo=TZ), True),    # start
        (datetime(2026, 8, 10, 14, 30, tzinfo=TZ), True),
        (datetime(2026, 8, 10, 15, 10, tzinfo=TZ), True),   # end
        (datetime(2026, 8, 10, 15, 11, tzinfo=TZ), False),  # after window
    ]
    for when, expected in checks:
        actual = alert_window_open(DEFAULT_AFTERNOON_CONFIG, now=when)
        print(f"   {when.isoformat()} -> {actual}")
        assert actual == expected
    print("   PASS")

    c = candidate()
    print("\n2) Alert-text construction")
    text = build_alert_text(c)
    assert "TESTBSE" in text
    assert "3.85" in text
    assert "82" in text
    print(text)
    print("   PASS")

    print("\n3) Direct ntfy transport test")
    ok, detail = send_ntfy_alert("This is a transport test from UltimateBullScanner.", c)
    print(f"   {ok}: {detail}")
    assert ok, detail
    print("   PASS — check your phone now")

    print("\n4) End-to-end simulated Monday 2:30 PM")
    os.environ["AFTERNOON_ALERT_TEST_MODE"] = "1"
    simulated = datetime(2026, 8, 10, 14, 30, tzinfo=TZ)
    logs = send_candidate_alerts(c, DEFAULT_AFTERNOON_CONFIG, now=simulated)
    for line in logs:
        print("   ", line)
    assert any("ntfy urgent notification sent" in x for x in logs), logs
    assert any("TEST MODE" in x for x in logs), logs
    print("   PASS — check your phone again")

    print("\nALL LOCAL ALERT TESTS PASSED")


if __name__ == "__main__":
    main()
