"""
Results-aware scoring overlay.

This does NOT replace the existing momentum score. It adds:
- A/B priority for results-day names.
- Results schedule confidence.
- Confirmed release bonus.
- Earnings-catalyst bonus when the existing catalyst engine reports it.
- Anti-chase penalty.
- A separate results_setup_score for ranking.

No claim of future return is made.
"""

from __future__ import annotations

from typing import Any, Dict


def group_priority(group: str) -> float:
    g = str(group or "").strip().upper()
    if g == "A":
        return 1.00
    if g == "B":
        return 0.95
    return 0.70


def extension_penalty(move_pct: float) -> float:
    """Penalty designed to avoid chasing already-extended rallies."""
    m = float(move_pct or 0.0)
    if m <= 5:
        return 0.0
    if m <= 8:
        return 3.0
    if m <= 10:
        return 7.0
    if m <= 12:
        return 12.0
    if m <= 15:
        return 18.0
    return 25.0


def results_priority_bonus(
    candidate: Dict[str, Any],
    results_scheduled_today: bool,
    release_confirmed: bool = False,
) -> float:
    if not results_scheduled_today:
        return 0.0

    group = str(candidate.get("group") or "").upper()
    base = 8.0 if group == "A" else 7.0 if group == "B" else 3.0

    if release_confirmed:
        base += 6.0

    catalyst = str(candidate.get("catalyst_strength") or "").upper()
    if catalyst == "STRONG":
        base += 5.0
    elif catalyst == "POSITIVE":
        base += 3.0
    elif catalyst == "CAUTION":
        base -= 8.0

    return base


def enrich_results_score(
    candidate: Dict[str, Any],
    results_scheduled_today: bool,
    release_confirmed: bool = False,
) -> Dict[str, Any]:
    move = float(candidate.get("window_move_pct") or 0.0)
    base_score = float(candidate.get("score") or 0.0)

    bonus = results_priority_bonus(
        candidate,
        results_scheduled_today,
        release_confirmed,
    )
    penalty = extension_penalty(move)

    # Keep score bounded. The base engine remains the dominant signal.
    fused = max(0.0, min(100.0, base_score + bonus - penalty))

    candidate["results_scheduled_today"] = bool(results_scheduled_today)
    candidate["results_release_confirmed"] = bool(release_confirmed)
    candidate["results_priority_bonus"] = round(bonus, 2)
    candidate["extension_penalty"] = round(penalty, 2)
    candidate["results_setup_score"] = round(fused, 2)

    if results_scheduled_today:
        candidate["results_priority"] = (
            "A_RESULTS_TODAY" if str(candidate.get("group")).upper() == "A"
            else "B_RESULTS_TODAY" if str(candidate.get("group")).upper() == "B"
            else "RESULTS_TODAY"
        )
    else:
        candidate["results_priority"] = "NORMAL"

    candidate["score_before_results_overlay"] = round(base_score, 2)
    candidate["score"] = round(fused, 2)

    return candidate
