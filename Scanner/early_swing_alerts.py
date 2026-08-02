"""Persistent alert manager for Early Swing candidates.

The alert layer never changes the scanner score. It classifies execution state,
prevents duplicate notifications for the same setup, and emits a new event only
for a new setup, a meaningful state transition, or a configured cooldown expiry.
"""
from __future__ import annotations

import csv
import hashlib
import json
from dataclasses import asdict
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Dict, Iterable, List, Mapping, Optional, Sequence, Tuple

from early_swing_config import DEFAULT_EARLY_SWING_CONFIG, EarlySwingConfig
from early_swing_models import AlertEvent, EarlySwingCandidate

STATE_VERSION = 1


def setup_key(candidate: EarlySwingCandidate) -> str:
    pivot = "NA" if candidate.pivot is None else f"{float(candidate.pivot):.4f}"
    raw = "|".join((candidate.symbol, candidate.setup_date, candidate.setup_type, pivot))
    digest = hashlib.sha1(raw.encode("utf-8")).hexdigest()[:16]
    return f"{candidate.symbol}:{digest}"


def _load_state(path: str | Path) -> dict:
    target = Path(path)
    if not target.exists():
        return {"version": STATE_VERSION, "setups": {}}
    try:
        payload = json.loads(target.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return {"version": STATE_VERSION, "setups": {}}
    if not isinstance(payload, dict) or payload.get("version") != STATE_VERSION:
        return {"version": STATE_VERSION, "setups": {}}
    payload.setdefault("setups", {})
    return payload


def _save_state(state: Mapping[str, object], path: str | Path) -> None:
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    temp = target.with_suffix(target.suffix + ".tmp")
    temp.write_text(json.dumps(state, indent=2, sort_keys=True), encoding="utf-8")
    temp.replace(target)


def classify_alert_status(candidate: EarlySwingCandidate, config: EarlySwingConfig) -> Tuple[str, str]:
    price = candidate.current_price
    if candidate.stop_loss is not None and price is not None and price < candidate.stop_loss:
        return "INVALIDATED", f"Price {price:.2f} is below stop {candidate.stop_loss:.2f}"
    if candidate.rejected:
        return "NOT_ACTIONABLE", candidate.rejection_reason or "Candidate rejected"
    if price is None or candidate.entry_low is None or candidate.entry_high is None:
        return "STILL_FORMING", "Entry range is not fully available"

    tolerance = config.alert_price_tolerance_pct / 100
    lower = candidate.entry_low * (1 - tolerance)
    upper = candidate.entry_high * (1 + tolerance)
    if price > upper:
        return "ABOVE_ENTRY_DO_NOT_CHASE", f"Price {price:.2f} is above maximum entry {candidate.entry_high:.2f}"
    if candidate.suggested_status == "Wait for Pullback":
        return "WAITING_FOR_PULLBACK", "Setup quality is acceptable but price needs a controlled pullback"
    if candidate.suggested_status == "Watch":
        return "STILL_FORMING", "Setup needs one more confirmation"
    if lower <= price <= upper and candidate.suggested_status == "Enter":
        return "READY_NEAR_ENTRY", f"Price {price:.2f} is inside the configured entry zone"
    if price < lower:
        return "STILL_FORMING", f"Price {price:.2f} has not reached the entry trigger"
    return "READY_NEAR_ENTRY", "Candidate is actionable near its entry zone"


def _eligible(candidate: EarlySwingCandidate, status: str, config: EarlySwingConfig) -> Tuple[bool, str]:
    if status not in {"READY_NEAR_ENTRY", "WAITING_FOR_PULLBACK", "STILL_FORMING", "ABOVE_ENTRY_DO_NOT_CHASE", "INVALIDATED"}:
        return False, "Status is not alertable"
    if status == "STILL_FORMING" and not config.alert_allow_watch_status:
        return False, "Watch alerts are disabled"
    if status in {"INVALIDATED", "ABOVE_ENTRY_DO_NOT_CHASE"}:
        # These are useful only as transitions for a setup that was already known.
        return True, "State-transition alert"
    if candidate.rules_score < config.alert_minimum_rules_score:
        return False, f"Rules score {candidate.rules_score:.1f} is below alert threshold"
    if config.alert_minimum_empirical_sample > 0:
        if candidate.empirical_sample_size < config.alert_minimum_empirical_sample:
            return False, "Empirical sample is below alert threshold"
    if config.alert_minimum_empirical_hit_rate_pct > 0:
        if candidate.empirical_hit_rate_pct is None or candidate.empirical_hit_rate_pct < config.alert_minimum_empirical_hit_rate_pct:
            return False, "Empirical hit rate is below alert threshold"
    return True, "Alert conditions satisfied"


def _parse_iso(value: object) -> Optional[datetime]:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value))
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed
    except ValueError:
        return None


def generate_alerts(
    candidates: Sequence[EarlySwingCandidate],
    config: EarlySwingConfig = DEFAULT_EARLY_SWING_CONFIG,
    state_path: Optional[str] = None,
    output_path: Optional[str] = None,
    now: Optional[datetime] = None,
) -> List[AlertEvent]:
    state_file = state_path or config.alert_state_file
    output_file = output_path or config.alert_output_file
    now = now or datetime.now(timezone.utc)
    state = _load_state(state_file)
    setups: Dict[str, dict] = state.setdefault("setups", {})
    events: List[AlertEvent] = []
    seen_keys = set()
    current_by_symbol = {candidate.symbol: candidate for candidate in candidates}

    for candidate in candidates:
        key = setup_key(candidate)
        seen_keys.add(key)
        status, status_reason = classify_alert_status(candidate, config)
        candidate.alert_status = status
        eligible, eligibility_reason = _eligible(candidate, status, config)
        candidate.alert_eligible = eligible
        candidate.alert_reason = status_reason if eligible else eligibility_reason

        previous = setups.get(key, {}) if isinstance(setups.get(key, {}), dict) else {}
        previous_status = str(previous.get("status") or "")
        last_alert_at = _parse_iso(previous.get("last_alert_at"))
        cooldown_expired = last_alert_at is None or now - last_alert_at >= timedelta(days=config.alert_cooldown_days)
        is_transition = bool(previous_status and previous_status != status)
        is_new = not previous_status

        should_emit = False
        if eligible:
            if status in {"INVALIDATED", "ABOVE_ENTRY_DO_NOT_CHASE"}:
                should_emit = bool(previous_status and is_transition)
            else:
                should_emit = is_new or is_transition or cooldown_expired

        if should_emit:
            event = AlertEvent(
                setup_key=key,
                symbol=candidate.symbol,
                ticker=candidate.ticker,
                company_name=candidate.company_name,
                setup_date=candidate.setup_date,
                setup_type=candidate.setup_type,
                alert_status=status,
                rules_score=candidate.rules_score,
                empirical_hit_rate_pct=candidate.empirical_hit_rate_pct,
                empirical_sample_size=candidate.empirical_sample_size,
                current_price=candidate.current_price,
                entry_low=candidate.entry_low,
                entry_high=candidate.entry_high,
                stop_loss=candidate.stop_loss,
                target1=candidate.target1,
                target2=candidate.target2,
                reason=status_reason,
                generated_at=now.isoformat(),
            )
            events.append(event)
            last_alert_value = now.isoformat()
        else:
            last_alert_value = previous.get("last_alert_at")

        setups[key] = {
            "symbol": candidate.symbol,
            "ticker": candidate.ticker,
            "company_name": candidate.company_name,
            "setup_date": candidate.setup_date,
            "setup_type": candidate.setup_type,
            "pivot": candidate.pivot,
            "entry_low": candidate.entry_low,
            "entry_high": candidate.entry_high,
            "stop_loss": candidate.stop_loss,
            "target1": candidate.target1,
            "target2": candidate.target2,
            "empirical_hit_rate_pct": candidate.empirical_hit_rate_pct,
            "empirical_sample_size": candidate.empirical_sample_size,
            "status": status,
            "rules_score": candidate.rules_score,
            "last_seen_at": now.isoformat(),
            "last_alert_at": last_alert_value,
            "active": status not in {"INVALIDATED", "NOT_ACTIONABLE"},
        }

    # A previously alerted setup may disappear from today's valid-candidate list
    # after breaking its stop. Preserve the prior technical levels in state so
    # that transition can still be reported instead of silently vanishing.
    for key, previous in list(setups.items()):
        if key in seen_keys or not isinstance(previous, dict) or not previous.get("active"):
            continue
        symbol = str(previous.get("symbol") or "")
        current = current_by_symbol.get(symbol)
        current_price = current.current_price if current is not None else None
        stop = previous.get("stop_loss")
        setup_date_value = str(previous.get("setup_date") or "")
        setup_date_parsed = None
        try:
            setup_date_parsed = datetime.fromisoformat(setup_date_value).date()
        except ValueError:
            pass

        invalidated = current_price is not None and stop is not None and float(current_price) < float(stop)
        expired = bool(
            setup_date_parsed
            and now.date() - setup_date_parsed > timedelta(days=config.alert_setup_expiry_days)
        )
        if invalidated:
            status = "INVALIDATED"
            reason = f"Price {float(current_price):.2f} is below stored stop {float(stop):.2f}"
        elif expired:
            status = "EXPIRED"
            reason = f"Setup is older than {config.alert_setup_expiry_days} calendar days"
        else:
            continue

        previous_status = str(previous.get("status") or "")
        if previous_status != status:
            events.append(AlertEvent(
                setup_key=key,
                symbol=symbol,
                ticker=str(previous.get("ticker") or (current.ticker if current else "")),
                company_name=str(previous.get("company_name") or (current.company_name if current else symbol)),
                setup_date=setup_date_value,
                setup_type=str(previous.get("setup_type") or "N/A"),
                alert_status=status,
                rules_score=float(previous.get("rules_score") or 0.0),
                empirical_hit_rate_pct=previous.get("empirical_hit_rate_pct"),
                empirical_sample_size=int(previous.get("empirical_sample_size") or 0),
                current_price=current_price,
                entry_low=previous.get("entry_low"),
                entry_high=previous.get("entry_high"),
                stop_loss=stop,
                target1=previous.get("target1"),
                target2=previous.get("target2"),
                reason=reason,
                generated_at=now.isoformat(),
            ))
        previous["status"] = status
        previous["active"] = False
        previous["last_seen_at"] = now.isoformat()
        previous["last_alert_at"] = now.isoformat()

    state["updated_at"] = now.isoformat()
    _save_state(state, state_file)
    write_alerts_csv(events, output_file)
    return events


def write_alerts_csv(events: Sequence[AlertEvent], path: str | Path) -> None:
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    rows = [asdict(event) for event in events]
    fieldnames = list(AlertEvent.__dataclass_fields__.keys())
    with target.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)
