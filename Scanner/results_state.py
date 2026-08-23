"""
Persistent state for results-aware alerts.

State is local JSON and is safe to delete for a clean test.
"""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from typing import Any, Dict


class ResultsAlertState:
    def __init__(self, path: str = "afternoon_reports/results_alert_state.json"):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.data: Dict[str, Any] = self._load()

    def _load(self) -> Dict[str, Any]:
        if not self.path.exists():
            return {}
        try:
            return json.loads(self.path.read_text(encoding="utf-8"))
        except Exception:
            return {}

    def save(self) -> None:
        self.path.write_text(
            json.dumps(self.data, indent=2, ensure_ascii=False, default=str),
            encoding="utf-8",
        )

    def mark(self, key: str, now: datetime, score: float, event: str) -> None:
        self.data[key] = {
            "time": now.isoformat(),
            "score": float(score),
            "event": event,
        }
        self.save()

    def get(self, key: str):
        return self.data.get(key)
