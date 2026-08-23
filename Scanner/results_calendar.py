"""
Results-calendar layer for the BSE afternoon scanner.

Design:
- Primary schedule source: BSE Board Meetings page (best effort).
- Optional local CSV override: results_calendar.csv.
- A scheduled board meeting is NOT treated as a confirmed result release.
- Exact release time remains unknown until a fresh BSE/company announcement is observed.

CSV columns accepted (case-insensitive):
security_code, security_id, isin, company_name, result_date, board_meeting_date,
expected_time, source, result_type
"""

from __future__ import annotations

import csv
import io
import re
from dataclasses import dataclass, asdict
from datetime import date, datetime
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional

import pandas as pd
import requests


BSE_BOARD_MEETING_URL = "https://www.bseindia.com/corporates/board_meeting.aspx/board_meeting.aspx"

RESULT_KEYWORDS = (
    "quarterly results",
    "financial results",
    "unaudited financial results",
    "audited financial results",
    "standalone financial results",
    "consolidated financial results",
    "results",
)


@dataclass(frozen=True)
class ResultsEvent:
    security_code: str = ""
    security_id: str = ""
    isin: str = ""
    company_name: str = ""
    result_date: str = ""
    board_meeting_date: str = ""
    expected_time: str = ""
    source: str = ""
    result_type: str = "RESULTS"
    release_confirmed: bool = False
    release_time: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


def _norm(value: Any) -> str:
    return str(value or "").strip()


def _key(value: Any) -> str:
    return _norm(value).upper().replace(" ", "").replace("-", "")


def _is_results_purpose(value: Any) -> bool:
    text = _norm(value).lower()
    return any(k in text for k in RESULT_KEYWORDS)


def _parse_date(value: Any) -> Optional[date]:
    if value is None or _norm(value) == "":
        return None
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    text = _norm(value)
    for fmt in ("%d %b %Y", "%d-%b-%Y", "%d/%m/%Y", "%Y-%m-%d", "%d %B %Y"):
        try:
            return datetime.strptime(text, fmt).date()
        except ValueError:
            pass
    try:
        return pd.to_datetime(text, dayfirst=True, errors="raise").date()
    except Exception:
        return None


def _first(row: Dict[str, Any], names: Iterable[str]) -> str:
    normalized = {
        re.sub(r"[^a-z0-9]", "", str(k).lower()): v
        for k, v in row.items()
    }
    for name in names:
        value = normalized.get(re.sub(r"[^a-z0-9]", "", name.lower()))
        if _norm(value):
            return _norm(value)
    return ""


def load_results_calendar(path: str = "results_calendar.csv") -> List[ResultsEvent]:
    """Load a local results calendar if supplied.

    This is deliberately supported because exchange schedule pages can change
    their HTML/API implementation without notice.
    """
    p = Path(path)
    if not p.exists():
        return []

    events: List[ResultsEvent] = []
    with p.open("r", encoding="utf-8-sig", newline="") as fh:
        for row in csv.DictReader(fh):
            result_date = _first(row, ["result_date", "results_date", "date"])
            board_date = _first(row, ["board_meeting_date", "meeting_date"])
            events.append(
                ResultsEvent(
                    security_code=_first(row, ["security_code", "scrip_code", "bse_code"]),
                    security_id=_first(row, ["security_id", "scrip_name", "symbol"]).upper(),
                    isin=_first(row, ["isin", "isin_no"]).upper(),
                    company_name=_first(row, ["company_name", "name", "security_name"]),
                    result_date=result_date,
                    board_meeting_date=board_date,
                    expected_time=_first(row, ["expected_time", "result_time", "time"]),
                    source=_first(row, ["source"]) or "LOCAL_CSV",
                    result_type=_first(row, ["result_type", "purpose"]) or "RESULTS",
                    release_confirmed=_first(row, ["release_confirmed"]).lower() in ("1", "true", "yes"),
                    release_time=_first(row, ["release_time"]),
                )
            )
    return events


def fetch_bse_board_meetings(
    target_date: date,
    timeout: int = 15,
) -> List[ResultsEvent]:
    """Best-effort scrape of BSE's public Board Meetings page.

    BSE exposes meeting date, security code/name and purpose on the public
    Board Meetings page. We filter to result-related purposes.

    If BSE changes the page or blocks the request, return [] rather than
    breaking the trading scanner.
    """
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                      "(KHTML, like Gecko) Chrome/151.0 Safari/537.36",
        "Referer": "https://www.bseindia.com/",
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    }

    try:
        response = requests.get(
            BSE_BOARD_MEETING_URL,
            headers=headers,
            timeout=timeout,
        )
        response.raise_for_status()
    except Exception as exc:
        print(f"[Results calendar] BSE board-meeting fetch failed: {exc}")
        return []

    try:
        tables = pd.read_html(io.StringIO(response.text))
    except Exception as exc:
        print(f"[Results calendar] Could not parse BSE board-meeting table: {exc}")
        return []

    events: List[ResultsEvent] = []
    target = target_date

    for table in tables:
        if table.empty:
            continue

        table.columns = [
            str(c).strip().lower().replace(" ", "_")
            for c in table.columns
        ]

        # BSE commonly exposes Security Code / Security Name / Purpose / Meeting Date.
        for _, raw in table.iterrows():
            row = {str(k): raw[k] for k in table.columns}

            code = _first(row, ["security_code", "scrip_code", "securitycode"])
            name = _first(row, ["security_name", "scrip_name", "securityname"])
            purpose = _first(row, ["purpose", "meeting_purpose"])
            meeting_date_raw = _first(row, ["meeting_date", "meetingdate"])

            meeting_date = _parse_date(meeting_date_raw)
            if not meeting_date or meeting_date != target:
                continue
            if not _is_results_purpose(purpose):
                continue

            events.append(
                ResultsEvent(
                    security_code=code,
                    security_id=name.upper(),
                    company_name=name,
                    result_date=meeting_date.isoformat(),
                    board_meeting_date=meeting_date.isoformat(),
                    source="BSE_BOARD_MEETING",
                    result_type=purpose or "RESULTS",
                )
            )

    # Deduplicate.
    dedup: Dict[str, ResultsEvent] = {}
    for event in events:
        k = _key(event.security_code) or _key(event.security_id) or _key(event.company_name)
        dedup[k] = event
    return list(dedup.values())


def get_today_results(
    target_date: date,
    calendar_path: str = "results_calendar.csv",
    timeout: int = 15,
) -> List[ResultsEvent]:
    """Merge local calendar and BSE schedule, preferring local entries."""
    bse = fetch_bse_board_meetings(target_date, timeout=timeout)
    local = load_results_calendar(calendar_path)

    selected: Dict[str, ResultsEvent] = {}

    for event in bse:
        event_date = _parse_date(event.result_date or event.board_meeting_date)
        if event_date == target_date:
            key = _key(event.security_code) or _key(event.security_id) or _key(event.company_name)
            if key:
                selected[key] = event

    for event in local:
        event_date = _parse_date(event.result_date or event.board_meeting_date)
        if event_date == target_date:
            key = _key(event.security_code) or _key(event.security_id) or _key(event.company_name)
            if key:
                selected[key] = event

    return list(selected.values())


def match_events_to_universe(
    universe: List[Dict[str, Any]],
    events: List[ResultsEvent],
) -> Dict[str, ResultsEvent]:
    """Map results events to scanner universe records."""
    by_code = {_key(x.get("security_code")): x for x in universe if x.get("security_code")}
    by_id = {_key(x.get("security_id")): x for x in universe if x.get("security_id")}
    by_isin = {_key(x.get("isin")): x for x in universe if x.get("isin")}
    by_name = {_key(x.get("name")): x for x in universe if x.get("name")}

    matched: Dict[str, ResultsEvent] = {}

    for event in events:
        stock = (
            by_code.get(_key(event.security_code))
            or by_id.get(_key(event.security_id))
            or by_isin.get(_key(event.isin))
            or by_name.get(_key(event.company_name))
        )
        if not stock:
            continue

        key = _key(stock.get("security_code")) or _key(stock.get("security_id")) or _key(stock.get("ticker"))
        matched[key] = event

    return matched
