"""Immediate alert delivery for afternoon fresh-move candidates.

Channels:
- Twilio SMS
- Twilio outbound voice call
- Twilio WhatsApp (approved ContentSid template preferred)
- Gmail fallback

Credentials are read only from environment variables / GitHub Secrets.  Missing
credentials never fail the scanner.  A hard IST clock guard ensures this module
will not send a trade alert outside the configured 2:00 PM-3:10 PM window.
"""
from __future__ import annotations

import html
import json
import os
import smtplib
from datetime import datetime, time
from email.message import EmailMessage
from typing import Any, Dict, List, Optional
from zoneinfo import ZoneInfo

import requests

from afternoon_config import AfternoonConfig, DEFAULT_AFTERNOON_CONFIG


def _env(name: str) -> str:
    return str(os.getenv(name, "")).strip()


def _test_mode() -> bool:
    """When enabled, only ntfy is sent; paid SMS/WhatsApp/call/email are skipped."""
    return _env("AFTERNOON_ALERT_TEST_MODE").lower() in {"1", "true", "yes", "on"}


def _as_e164(number: str) -> str:
    """Normalize harmless whitespace but keep the caller responsible for E.164."""
    return "".join(str(number or "").split())


def _clock(value: str) -> time:
    return datetime.strptime(value, "%H:%M").time()


def send_ntfy_alert(
    text: str,
    candidate: Dict[str, Any],
) -> tuple[bool, str]:
    """Send an urgent ntfy push notification.

    NTFY_TOPIC should be supplied through the environment rather than committed
    to source control. The user's current topic can be configured locally as:
        NTFY_TOPIC=ubs_afternoon_7f92a6c814

    Priority 5 is ntfy's highest/urgent priority.
    """
    topic = _env("NTFY_TOPIC")
    if not topic:
        return False, "ntfy topic not configured (set NTFY_TOPIC)"

    topic = topic.strip().strip("/")
    if not topic:
        return False, "ntfy topic is empty"

    # Prevent accidental path injection. ntfy topic names should not contain
    # whitespace or URL path separators for this simple public-server setup.
    if any(ch.isspace() for ch in topic) or "/" in topic or "\\" in topic:
        return False, "invalid ntfy topic: use a single random topic name"

    symbol = str(candidate.get("security_id") or candidate.get("symbol") or "BSE")
    score = candidate.get("score")

    def fmt(value: Any, digits: int = 2) -> str:
        try:
            return f"{float(value):.{digits}f}"
        except (TypeError, ValueError):
            return "NA"

    title = f"BSE FRESH MOVE - {symbol}"
    message = (
        "🚨 ACTIONABLE 2PM FRESH MOVE\n\n"
        f"{text}\n\n"
        f"Score: {fmt(score, 0)}/100\n"
        "Review chart/liquidity before ordering. No return is guaranteed."
    )

    headers = {
        "Title": title,
        "Priority": "5",
        "Tags": "rotating_light,chart_with_upwards_trend,moneybag",
    }

    url = f"https://ntfy.sh/{topic}"
    try:
        response = requests.post(
            url,
            data=message.encode("utf-8"),
            headers=headers,
            timeout=15,
        )
        if 200 <= response.status_code < 300:
            return True, "ntfy urgent notification sent"
        return False, f"ntfy HTTP {response.status_code}: {response.text[:250]}"
    except Exception as exc:
        return False, f"ntfy error: {exc}"


def alert_window_open(
    config: AfternoonConfig = DEFAULT_AFTERNOON_CONFIG,
    now: Optional[datetime] = None,
) -> bool:
    """True only inside the configured live-alert window in India time."""
    tz = ZoneInfo(config.timezone)
    if now is None:
        local_now = datetime.now(tz)
    elif now.tzinfo is None:
        local_now = now.replace(tzinfo=tz)
    else:
        local_now = now.astimezone(tz)

    # Do not send live market alerts on weekends even if --once is run manually.
    if local_now.weekday() >= 5:
        return False

    current = local_now.time().replace(tzinfo=None, second=0, microsecond=0)
    return _clock(config.window_start) <= current <= _clock(config.window_end)


def build_alert_text(candidate: Dict[str, Any]) -> str:
    score = candidate.get("score")
    symbol = candidate.get("security_id") or candidate.get("symbol") or "UNKNOWN"
    name = candidate.get("name", "")
    price = candidate.get("latest_price")
    move = candidate.get("window_move_pct")
    rvol = candidate.get("same_window_rvol")
    reason = candidate.get("catalyst_reason") or "Technical momentum"
    entry = candidate.get("entry")
    stop = candidate.get("stop_loss")
    target1 = candidate.get("target1")
    target2 = candidate.get("target2")
    hit8 = candidate.get("hist_hit_8_pct")
    hit15 = candidate.get("hist_hit_15_pct")
    hit20 = candidate.get("hist_hit_20_pct")

    parts = [f"BSE 2PM FRESH MOVE: {symbol} {name}".strip()]
    if price is not None:
        parts.append(f"price {price:.2f}")
    if move is not None:
        parts.append(f"2PM move +{move:.2f}%")
    if rvol is not None:
        parts.append(f"same-time RVOL {rvol:.2f}x")
    if score is not None:
        parts.append(f"score {score:.0f}/100")
    parts.append(f"reason: {reason}")
    if entry is not None and stop is not None:
        parts.append(f"ENTRY REVIEW {entry:.2f} | STOP {stop:.2f}")
    if target1 is not None:
        parts.append(f"T1 {target1:.2f}")
    if target2 is not None:
        parts.append(f"T2 {target2:.2f}")
    if hit8 is not None:
        parts.append(f"historical +8%/3d hit {hit8:.0f}%")
    if hit15 is not None or hit20 is not None:
        h15 = "NA" if hit15 is None else f"{hit15:.0f}%"
        h20 = "NA" if hit20 is None else f"{hit20:.0f}%"
        parts.append(f"historical +15/+20% in 3d {h15}/{h20}")
    parts.append("Review chart/liquidity before ordering; no return is guaranteed")
    return ". ".join(parts)


def _whatsapp_template_variables(candidate: Dict[str, Any]) -> Dict[str, str]:
    """Variables for the recommended Twilio WhatsApp Content Template.

    Suggested template body:
      BSE trade alert {{1}} | score {{2}}/100 | 2PM move {{3}}% | RVOL {{4}}x.
      Entry {{5}} | Stop {{6}} | T1 {{7}}. Reason: {{8}}
    """
    symbol = str(candidate.get("security_id") or candidate.get("symbol") or "UNKNOWN")
    score = candidate.get("score")
    move = candidate.get("window_move_pct")
    rvol = candidate.get("same_window_rvol")
    entry = candidate.get("entry")
    stop = candidate.get("stop_loss")
    target1 = candidate.get("target1")
    reason = str(candidate.get("catalyst_reason") or "Technical momentum")[:120]

    def f(value: Any, digits: int = 2) -> str:
        try:
            return f"{float(value):.{digits}f}"
        except (TypeError, ValueError):
            return "NA"

    return {
        "1": symbol,
        "2": f(score, 0),
        "3": f(move, 2),
        "4": f(rvol, 2),
        "5": f(entry, 2),
        "6": f(stop, 2),
        "7": f(target1, 2),
        "8": reason,
    }


def send_twilio_sms(text: str) -> tuple[bool, str]:
    sid = _env("TWILIO_ACCOUNT_SID")
    token = _env("TWILIO_AUTH_TOKEN")
    from_number = _as_e164(_env("TWILIO_FROM_NUMBER"))
    to_number = _as_e164(_env("ALERT_PHONE_NUMBER"))
    if not all((sid, token, from_number, to_number)):
        return False, "Twilio SMS credentials not configured"
    url = f"https://api.twilio.com/2010-04-01/Accounts/{sid}/Messages.json"
    try:
        response = requests.post(
            url,
            auth=(sid, token),
            data={"To": to_number, "From": from_number, "Body": text[:1500]},
            timeout=15,
        )
        if 200 <= response.status_code < 300:
            payload = response.json() if response.content else {}
            return True, f"Twilio SMS queued ({payload.get('sid', 'no-sid')})"
        return False, f"Twilio SMS HTTP {response.status_code}: {response.text[:250]}"
    except Exception as exc:
        return False, f"Twilio SMS error: {exc}"


def send_twilio_whatsapp(text: str, candidate: Dict[str, Any]) -> tuple[bool, str]:
    """Send WhatsApp through Twilio.

    For unattended market alerts, configure TWILIO_WHATSAPP_CONTENT_SID with an
    approved WhatsApp template.  Free-form Body mode is intentionally opt-in
    because it normally works only inside an open 24-hour customer-service
    session.
    """
    sid = _env("TWILIO_ACCOUNT_SID")
    token = _env("TWILIO_AUTH_TOKEN")
    from_number = _as_e164(_env("TWILIO_WHATSAPP_FROM"))
    to_number = _as_e164(_env("ALERT_PHONE_NUMBER"))
    content_sid = _env("TWILIO_WHATSAPP_CONTENT_SID")
    allow_freeform = _env("TWILIO_WHATSAPP_ALLOW_FREEFORM").lower() in {"1", "true", "yes", "on"}

    if not all((sid, token, from_number, to_number)):
        return False, "Twilio WhatsApp credentials not configured"

    if not from_number.startswith("whatsapp:"):
        from_number = f"whatsapp:{from_number}"
    if not to_number.startswith("whatsapp:"):
        to_number = f"whatsapp:{to_number}"

    data: Dict[str, str] = {"To": to_number, "From": from_number}
    if content_sid:
        data["ContentSid"] = content_sid
        data["ContentVariables"] = json.dumps(_whatsapp_template_variables(candidate), separators=(",", ":"))
    elif allow_freeform:
        data["Body"] = text[:1500]
    else:
        return False, "WhatsApp sender configured but TWILIO_WHATSAPP_CONTENT_SID missing"

    url = f"https://api.twilio.com/2010-04-01/Accounts/{sid}/Messages.json"
    try:
        response = requests.post(url, auth=(sid, token), data=data, timeout=15)
        if 200 <= response.status_code < 300:
            payload = response.json() if response.content else {}
            mode = "template" if content_sid else "freeform"
            return True, f"Twilio WhatsApp {mode} queued ({payload.get('sid', 'no-sid')})"
        return False, f"Twilio WhatsApp HTTP {response.status_code}: {response.text[:250]}"
    except Exception as exc:
        return False, f"Twilio WhatsApp error: {exc}"


def send_twilio_call(text: str) -> tuple[bool, str]:
    sid = _env("TWILIO_ACCOUNT_SID")
    token = _env("TWILIO_AUTH_TOKEN")
    from_number = _as_e164(_env("TWILIO_FROM_NUMBER"))
    to_number = _as_e164(_env("ALERT_PHONE_NUMBER"))
    if not all((sid, token, from_number, to_number)):
        return False, "Twilio voice credentials not configured"
    url = f"https://api.twilio.com/2010-04-01/Accounts/{sid}/Calls.json"
    spoken = html.escape(text[:1100])
    twiml = f'<Response><Say voice="alice">{spoken}</Say></Response>'
    try:
        response = requests.post(
            url,
            auth=(sid, token),
            data={"To": to_number, "From": from_number, "Twiml": twiml},
            timeout=15,
        )
        if 200 <= response.status_code < 300:
            payload = response.json() if response.content else {}
            return True, f"Twilio call queued ({payload.get('sid', 'no-sid')})"
        return False, f"Twilio call HTTP {response.status_code}: {response.text[:250]}"
    except Exception as exc:
        return False, f"Twilio call error: {exc}"


def send_gmail_alert(text: str, candidate: Dict[str, Any]) -> tuple[bool, str]:
    sender = _env("GMAIL_SENDER")
    password = _env("GMAIL_APP_PASSWORD")
    recipient = _env("REPORT_RECIPIENT") or _env("ALERT_EMAIL")
    if not all((sender, password, recipient)):
        return False, "Gmail alert credentials not configured"
    symbol = candidate.get("security_id") or candidate.get("symbol") or "BSE"
    score = candidate.get("score", 0)
    msg = EmailMessage()
    msg["Subject"] = f"Afternoon fresh-move alert: {symbol} | score {score}"
    msg["From"] = sender
    msg["To"] = recipient
    msg.set_content(text)
    try:
        with smtplib.SMTP_SSL("smtp.gmail.com", 465, timeout=20) as smtp:
            smtp.login(sender, password)
            smtp.send_message(msg)
        return True, "Gmail alert sent"
    except Exception as exc:
        return False, f"Gmail alert error: {exc}"


def send_candidate_alerts(
    candidate: Dict[str, Any],
    config: AfternoonConfig = DEFAULT_AFTERNOON_CONFIG,
    now: Optional[datetime] = None,
) -> List[str]:
    """Send immediate channels for an actionable candidate.

    >= sms_alert_score: SMS + WhatsApp + email when configured.
    >= call_alert_score: voice call too.

    The hard time guard means even a manual --once execution cannot accidentally
    send a trade alert before 2:00 PM or after 3:10 PM IST.
    """
    if not alert_window_open(config, now=now):
        return [f"Alert suppressed: outside {config.window_start}-{config.window_end} {config.timezone}"]

    text = build_alert_text(candidate)
    score = float(candidate.get("score") or 0)
    logs: List[str] = []

    if score >= config.sms_alert_score:
        # FREE urgent push notification — primary alert channel.
        ok, detail = send_ntfy_alert(text, candidate)
        logs.append(detail)

        if _test_mode():
            logs.append("TEST MODE: Twilio SMS/WhatsApp/Gmail skipped")
        else:
            # Optional paid/trial channels. Missing credentials never stop ntfy.
            ok, detail = send_twilio_sms(text)
            logs.append(detail)

            ok, detail = send_twilio_whatsapp(text, candidate)
            logs.append(detail)

            ok, detail = send_gmail_alert(text, candidate)
            logs.append(detail)

    catalyst = str(candidate.get("catalyst_strength") or "NONE")
    catalyst_ok = catalyst not in ("NONE", "UNKNOWN", "TECHNICAL_OR_UNKNOWN", "CAUTION")
    if score >= config.call_alert_score and (not config.require_catalyst_for_call or catalyst_ok):
        if _test_mode():
            logs.append("TEST MODE: Twilio voice call skipped")
        else:
            ok, detail = send_twilio_call(text)
            logs.append(detail)

    return logs
