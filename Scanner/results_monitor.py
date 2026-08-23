"""
Live results watchlist manager.

The monitor is intentionally lightweight:
- Refreshes the day's scheduled result list periodically.
- Maps it to the existing 2,311-stock universe.
- Provides A/B priority.
- Does not claim a result has been released until the existing BSE announcement
  layer confirms it.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta
from typing import Any, Dict, List, Optional

from results_calendar import (
    ResultsEvent,
    get_today_results,
    match_events_to_universe,
)


class ResultsMonitor:
    def __init__(
        self,
        universe: List[Dict[str, Any]],
        calendar_path: str = "results_calendar.csv",
        refresh_minutes: int = 10,
        timeout_seconds: int = 15,
    ):
        self.universe = universe
        self.calendar_path = calendar_path
        self.refresh_minutes = refresh_minutes
        self.timeout_seconds = timeout_seconds
        self._events: Dict[str, ResultsEvent] = {}
        self._loaded_at: Optional[datetime] = None
        self._loaded_date: Optional[date] = None

    def refresh(self, now: datetime, force: bool = False) -> Dict[str, ResultsEvent]:
        target_date = now.date()

        if (
            not force
            and self._loaded_at is not None
            and self._loaded_date == target_date
            and now - self._loaded_at < timedelta(minutes=self.refresh_minutes)
        ):
            return self._events

        events = get_today_results(
            target_date,
            calendar_path=self.calendar_path,
            timeout=self.timeout_seconds,
        )
        self._events = match_events_to_universe(self.universe, events)
        self._loaded_at = now
        self._loaded_date = target_date

        a = sum(1 for e in self._events.values() if False)  # kept harmless for compatibility
        print(
            f"[Results monitor] {len(events)} scheduled result events | "
            f"{len(self._events)} matched to scanner universe"
        )
        return self._events

    def watchlist(self, now: datetime) -> List[Dict[str, Any]]:
        events = self.refresh(now)
        rows = []

        for stock in self.universe:
            key = str(stock.get("security_code") or stock.get("security_id") or stock.get("ticker") or "").upper()
            event = events.get(key)
            if not event:
                continue

            row = dict(stock)
            row["results_scheduled_today"] = True
            row["results_result_date"] = event.result_date
            row["results_board_meeting_date"] = event.board_meeting_date
            row["results_expected_time"] = event.expected_time
            row["results_source"] = event.source
            row["results_type"] = event.result_type
            row["results_release_confirmed"] = event.release_confirmed
            row["results_release_time"] = event.release_time
            rows.append(row)

        rows.sort(
            key=lambda x: (
                str(x.get("group") or "").upper() not in ("A", "B"),
                str(x.get("group") or "").upper() != "A",
                str(x.get("security_id") or ""),
            )
        )
        return rows

    def event_for_stock(self, stock: Dict[str, Any], now: datetime) -> Optional[ResultsEvent]:
        events = self.refresh(now)
        keys = [
            str(stock.get("security_code") or "").upper(),
            str(stock.get("security_id") or "").upper(),
            str(stock.get("isin") or "").upper(),
        ]
        for key in keys:
            if key and key in events:
                return events[key]
        return None
