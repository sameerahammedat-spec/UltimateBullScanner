"""
Synthetic test for the Results Catalyst layer.

No Yahoo, BSE, NTFY, Twilio or Gmail calls are made.
"""

from datetime import datetime
from zoneinfo import ZoneInfo

from results_calendar import ResultsEvent, match_events_to_universe
from results_score import enrich_results_score, extension_penalty


TZ = ZoneInfo("Asia/Kolkata")


def main():
    print("=" * 72)
    print("RESULTS CATALYST SYNTHETIC TEST")
    print("=" * 72)

    universe = [
        {
            "security_code": "500001",
            "security_id": "TESTA",
            "name": "Test A Ltd",
            "group": "A",
            "ticker": "TESTA.BO",
        },
        {
            "security_code": "500002",
            "security_id": "TESTB",
            "name": "Test B Ltd",
            "group": "B",
            "ticker": "TESTB.BO",
        },
        {
            "security_code": "500003",
            "security_id": "TESTN",
            "name": "Normal Ltd",
            "group": "",
            "ticker": "TESTN.BO",
        },
    ]

    today = datetime(2026, 8, 10, 14, 30, tzinfo=TZ).date()

    events = [
        ResultsEvent(
            security_code="500001",
            security_id="TESTA",
            company_name="Test A Ltd",
            result_date=today.isoformat(),
            board_meeting_date=today.isoformat(),
            source="SYNTHETIC",
            result_type="Quarterly Results",
        ),
        ResultsEvent(
            security_code="500002",
            security_id="TESTB",
            company_name="Test B Ltd",
            result_date=today.isoformat(),
            board_meeting_date=today.isoformat(),
            source="SYNTHETIC",
            result_type="Quarterly Results",
        ),
    ]

    matched = match_events_to_universe(universe, events)
    assert len(matched) == 2
    print("1. Calendar-to-universe matching: PASS")

    a = {
        **universe[0],
        "score": 78,
        "window_move_pct": 5.7,
        "catalyst_strength": "STRONG",
    }
    b = {
        **universe[1],
        "score": 78,
        "window_move_pct": 5.7,
        "catalyst_strength": "STRONG",
    }
    normal = {
        **universe[2],
        "score": 78,
        "window_move_pct": 5.7,
        "catalyst_strength": "NONE",
    }

    enrich_results_score(a, True, False)
    enrich_results_score(b, True, True)
    enrich_results_score(normal, False, False)

    assert a["results_priority"] == "A_RESULTS_TODAY"
    assert b["results_priority"] == "B_RESULTS_TODAY"
    assert b["score"] > a["score"]
    assert normal["results_priority"] == "NORMAL"

    print(f"2. A results-day score : {a['score']}")
    print(f"3. B confirmed-result score : {b['score']}")
    print(f"4. Normal score : {normal['score']}")
    print("5. Priority/score fusion: PASS")

    assert extension_penalty(4) == 0
    assert extension_penalty(16) > extension_penalty(8)
    print("6. Anti-chase penalty: PASS")

    print()
    print("ALL RESULTS CATALYST TESTS PASSED")
    print("No external market data was queried.")


if __name__ == "__main__":
    main()
