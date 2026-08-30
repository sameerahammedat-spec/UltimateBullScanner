"""Pre-results reaction watcher (additive to the existing afternoon scanner).

This module deliberately does NOT modify the existing ticker resolver, afternoon
scanner, scoring engine, or alert code. It runs as a separate process and uses
the canonical universe/resolution layer to monitor companies with BSE-scheduled
results today and in the next few days.

It is designed for Jenkins: start at 13:30 IST, sample 5-minute data through
15:10 IST, send its own NTFY alerts for unusually strong pre-results reactions,
and write a separate EOD report after 16:00 IST.
"""
from __future__ import annotations

import argparse
import json
import math
import os
import time
from datetime import date, datetime, timedelta, time as dt_time
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence
from zoneinfo import ZoneInfo

import numpy as np
import pandas as pd
import requests
import yfinance as yf

from afternoon_fresh_move_scanner import load_afternoon_universe, _read_csv_robust
from market_scanner import _SHARED_SESSION
from ticker_resolver import is_equity_company, resolve_history, has_usable_ohlcv
from results_calendar import get_today_results, match_events_to_universe
from results_state import ResultsAlertState

TZ = ZoneInfo("Asia/Kolkata")
START = dt_time(13, 30)
ALERT_START = dt_time(14, 0)
END = dt_time(15, 10)
EOD = dt_time(16, 0)
DEFAULT_OUTPUT = "afternoon_reports"
DEFAULT_THRESHOLD = 72.0
DEFAULT_MOVE = 1.25
DEFAULT_LOOKAHEAD = 3
DEFAULT_COOLDOWN = 30


def _key(stock: Dict[str, Any]) -> str:
    return str(stock.get("security_code") or stock.get("isin") or stock.get("security_id") or stock.get("ticker") or "").upper()


def _safe_float(v: Any, default: float = 0.0) -> float:
    try:
        x = float(v)
        return x if math.isfinite(x) else default
    except (TypeError, ValueError):
        return default


def _load_events(universe: List[Dict[str, Any]], now: datetime, lookahead: int) -> Dict[str, Dict[str, Any]]:
    events: Dict[str, Dict[str, Any]] = {}
    for offset in range(0, lookahead + 1):
        target = now.date() + timedelta(days=offset)
        try:
            raw = get_today_results(target, calendar_path=os.getenv("RESULTS_CALENDAR_PATH", "results_calendar.csv"), timeout=int(os.getenv("RESULTS_REQUEST_TIMEOUT", "15")))
            matched = match_events_to_universe(universe, raw)
        except Exception as exc:
            print(f"[Results pre-event] calendar fetch failed for {target}: {exc}")
            continue
        for key, event in matched.items():
            stock = next((s for s in universe if _key(s) == key), None)
            if not stock or not is_equity_company(stock):
                continue
            days = (target - now.date()).days
            row = dict(stock)
            row.update({
                "result_event_date": target.isoformat(),
                "days_to_result": days,
                "result_bucket": "TODAY" if days == 0 else "TOMORROW" if days == 1 else f"UPCOMING_{days}D",
                "result_expected_time": event.expected_time,
                "result_source": event.source,
                "result_type": event.result_type,
                "result_release_confirmed": event.release_confirmed,
                "result_release_time": event.release_time,
                "result_event": event,
            })
            # Prefer the nearest event if duplicate records exist.
            prev = events.get(key)
            if prev is None or days < prev["days_to_result"]:
                events[key] = row
    return events


def _normalise(frame: pd.DataFrame) -> pd.DataFrame:
    if frame is None or frame.empty:
        return pd.DataFrame()
    f = frame.copy()
    if isinstance(f.columns, pd.MultiIndex):
        f.columns = f.columns.get_level_values(0)
    f.columns = [str(c).strip().title() for c in f.columns]
    for c in ("Open", "High", "Low", "Close", "Volume"):
        if c in f.columns:
            f[c] = pd.to_numeric(f[c], errors="coerce")
    return f.dropna(subset=[c for c in ("Open", "High", "Low", "Close") if c in f.columns])


def _fetch5(stock: Dict[str, Any]) -> tuple[str, pd.DataFrame]:
    ticker, frame, status = resolve_history(
        yf, stock, period=os.getenv("RESULTS_INTRADAY_PERIOD", "5d"),
        interval="5m", session=_SHARED_SESSION, retries=1,
    )
    return ticker, _normalise(frame) if status == "RESOLVED" else pd.DataFrame()


def _fetch1d(stock: Dict[str, Any], ticker: str) -> pd.DataFrame:
    # Once a source ticker is selected, daily history is fetched from that exact
    # ticker. This is an explicit source invariant for this add-on.
    try:
        obj = yf.Ticker(ticker, session=_SHARED_SESSION)
        return _normalise(obj.history(period="15d", interval="1d", auto_adjust=False))
    except Exception:
        try:
            return _normalise(yf.Ticker(ticker).history(period="15d", interval="1d", auto_adjust=False))
        except Exception:
            return pd.DataFrame()


def _score(frame: pd.DataFrame, now: datetime, days_to_result: int) -> Dict[str, Any]:
    if frame.empty or "Close" not in frame.columns:
        return {"score": 0.0, "move_pct": 0.0, "r15_pct": 0.0, "volume_ratio": 0.0, "status": "NO_INTRADAY_DATA"}
    local = frame.copy()
    idx = pd.to_datetime(local.index)
    if idx.tz is None:
        idx = idx.tz_localize("Asia/Kolkata")
    else:
        idx = idx.tz_convert("Asia/Kolkata")
    local.index = idx
    today = local[local.index.date == now.date()]
    watch = today.between_time("13:30", "15:10")
    if watch.empty:
        return {"score": 0.0, "move_pct": 0.0, "r15_pct": 0.0, "volume_ratio": 0.0, "status": "NO_TODAY_BARS"}
    close = watch["Close"].dropna()
    if len(close) < 2:
        return {"score": 0.0, "move_pct": 0.0, "r15_pct": 0.0, "volume_ratio": 0.0, "status": "INSUFFICIENT_BARS"}
    base = float(close.iloc[0])
    last = float(close.iloc[-1])
    move = (last / base - 1.0) * 100.0 if base else 0.0
    r15 = (float(close.iloc[-1]) / float(close.iloc[-4]) - 1.0) * 100.0 if len(close) >= 4 and close.iloc[-4] else 0.0
    volume_ratio = 0.0
    if "Volume" in watch.columns:
        vols = watch["Volume"].dropna()
        if len(vols) >= 8:
            current = float(vols.tail(3).mean())
            baseline = float(vols.iloc[:-3].tail(15).mean())
            if baseline > 0:
                volume_ratio = current / baseline
    # Bounded, explainable pre-results score. This is intentionally separate
    # from the production afternoon score.
    move_component = min(40.0, max(0.0, move) * 10.0)
    accel_component = min(20.0, max(0.0, r15) * 6.0)
    volume_component = min(20.0, max(0.0, volume_ratio - 1.0) * 8.0)
    timing_component = 15.0 if days_to_result == 0 else 13.0 if days_to_result == 1 else 8.0
    score = min(100.0, move_component + accel_component + volume_component + timing_component)
    return {
        "score": round(score, 2), "move_pct": round(move, 3), "r15_pct": round(r15, 3),
        "volume_ratio": round(volume_ratio, 2), "latest_price": round(last, 3),
        "status": "QUALIFIED" if score >= DEFAULT_THRESHOLD and move >= DEFAULT_MOVE else "WATCH",
    }


def _prior_context(daily: pd.DataFrame) -> Dict[str, Any]:
    if daily.empty or "Close" not in daily.columns:
        return {"prior_1d_pct": None, "prior_2d_pct": None, "prior_3d_pct": None}
    c = daily["Close"].dropna()
    if len(c) < 2:
        return {"prior_1d_pct": None, "prior_2d_pct": None, "prior_3d_pct": None}
    def ret(n: int) -> Optional[float]:
        if len(c) <= n:
            return None
        return round((float(c.iloc[-1]) / float(c.iloc[-1-n]) - 1) * 100, 3)
    return {"prior_1d_pct": ret(1), "prior_2d_pct": ret(2), "prior_3d_pct": ret(3)}


def _send_ntfy(row: Dict[str, Any]) -> tuple[bool, str]:
    topic = os.getenv("NTFY_TOPIC", "").strip().strip("/")
    if not topic:
        return False, "NTFY_TOPIC not configured"
    symbol = row.get("security_id") or row.get("ticker") or "UNKNOWN"
    title = f"PRE-RESULT REACTION - {symbol}"
    msg = (
        f"{symbol} {row.get('name','')}\n"
        f"Result: {row.get('result_bucket')} ({row.get('result_event_date')})\n"
        f"Move since 13:30: +{_safe_float(row.get('move_pct')):.2f}%\n"
        f"15m: +{_safe_float(row.get('r15_pct')):.2f}% | Vol ratio: {_safe_float(row.get('volume_ratio')):.2f}x\n"
        f"Pre-result score: {_safe_float(row.get('score')):.0f}/100\n"
        f"Prior 1/2/3d: {row.get('prior_1d_pct','NA')}% / {row.get('prior_2d_pct','NA')}% / {row.get('prior_3d_pct','NA')}%\n"
        "Pre-results reaction signal — review before acting."
    )
    try:
        r = requests.post(
            f"https://ntfy.sh/{topic}", data=msg.encode("utf-8"),
            headers={"Title": title, "Priority": "4", "Tags": "chart_with_upwards_trend,chart_with_upwards_trend"}, timeout=15,
        )
        if 200 <= r.status_code < 300:
            return True, "NTFY_SENT"
        return False, f"NTFY_HTTP_{r.status_code}"
    except Exception as exc:
        return False, f"NTFY_ERROR:{exc}"


class ResultsPreEventScanner:
    def __init__(self, universe: List[Dict[str, Any]], output_dir: str, lookahead: int = DEFAULT_LOOKAHEAD):
        self.universe = [x for x in universe if is_equity_company(x)]
        self.output_dir = Path(output_dir)
        self.output_dir.mkdir(parents=True, exist_ok=True)
        self.lookahead = lookahead
        self.events: Dict[str, Dict[str, Any]] = {}
        self.ledger: Dict[str, Dict[str, Any]] = {}
        self.alert_state: Dict[str, datetime] = {}
        self.state = ResultsAlertState(str(self.output_dir / "results_pre_event_alert_state.json"))

    def refresh_events(self, now: datetime) -> None:
        self.events = _load_events(self.universe, now, self.lookahead)
        print(f"[Results pre-event] scheduled companies today..+{self.lookahead}d: {len(self.events)}")

    def cycle(self, now: datetime) -> None:
        self.refresh_events(now)
        for key, event_stock in self.events.items():
            ticker, intraday = _fetch5(event_stock)
            if intraday.empty:
                row = dict(event_stock)
                row.update({"ticker": ticker or event_stock.get("ticker", ""), "status": "NO_INTRADAY_DATA", "move_pct": 0.0, "score": 0.0, "ntfy_status": "NOT_SENT", "alert_reason": "No usable 5m Yahoo data"})
            else:
                row = dict(event_stock)
                row["ticker"] = ticker
                row.update(_score(intraday, now, int(event_stock["days_to_result"])))
                daily = _fetch1d(event_stock, ticker)
                row.update(_prior_context(daily))
                row["source_ticker"] = ticker
                row["ntfy_status"] = "NOT_SENT"
                row["alert_reason"] = "Below pre-result alert threshold"
                if now.time().replace(second=0, microsecond=0) >= ALERT_START and now.time().replace(second=0, microsecond=0) <= END:
                    last = self.alert_state.get(key)
                    persisted = self.state.get(key) or {}
                    if last is None and persisted.get("time"):
                        try:
                            last = datetime.fromisoformat(str(persisted["time"]))
                        except Exception:
                            last = None
                    eligible = row["score"] >= DEFAULT_THRESHOLD and row["move_pct"] >= DEFAULT_MOVE
                    cooldown_ok = last is None or (now - last).total_seconds() >= DEFAULT_COOLDOWN * 60
                    if eligible and cooldown_ok:
                        sent, reason = _send_ntfy(row)
                        row["ntfy_status"] = reason if sent else "NOT_SENT"
                        row["alert_reason"] = "Qualified pre-result reaction" if sent else reason
                        if sent:
                            self.alert_state[key] = now
                            self.state.mark(key, now, row["score"], "PRE_RESULT_NTFY")
                    elif not eligible:
                        row["alert_reason"] = f"Score {row['score']:.1f} < {DEFAULT_THRESHOLD} or move {row['move_pct']:.2f}% < {DEFAULT_MOVE}%"
                    else:
                        row["alert_reason"] = "Alert cooldown active"
            row["observed_at"] = now.isoformat()
            existing = self.ledger.get(key)
            if existing is None or _safe_float(row.get("score")) >= _safe_float(existing.get("score")):
                if existing and existing.get("ntfy_status") == "NTFY_SENT" and row.get("ntfy_status") != "NTFY_SENT":
                    row["ntfy_status"] = "NTFY_SENT"
                    row["alert_reason"] = existing.get("alert_reason") or row.get("alert_reason")
                self.ledger[key] = row
            else:
                # Preserve best score and sticky alert delivery state.
                if existing.get("ntfy_status") == "NTFY_SENT":
                    row["ntfy_status"] = "NTFY_SENT"
                    row["alert_reason"] = existing.get("alert_reason") or row.get("alert_reason")
                existing.update({
                    "observed_at": now.isoformat(),
                    "ntfy_status": row.get("ntfy_status", existing.get("ntfy_status")),
                    "alert_reason": row.get("alert_reason", existing.get("alert_reason")),
                })

    def write_report(self, now: datetime) -> Path:
        rows = []
        for row in self.ledger.values():
            out = dict(row)
            out.pop("result_event", None)
            rows.append(out)
        rows.sort(key=lambda x: (-_safe_float(x.get("score")), int(x.get("days_to_result", 99))))
        df = pd.DataFrame(rows)
        day = now.astimezone(TZ).date().isoformat()
        xlsx = self.output_dir / f"results_pre_event_eod_{day}.xlsx"
        csv = self.output_dir / f"results_pre_event_eod_{day}.csv"
        js = self.output_dir / f"results_pre_event_eod_{day}.json"
        df.to_csv(csv, index=False)
        js.write_text(json.dumps(rows, indent=2, ensure_ascii=False, default=str), encoding="utf-8")
        with pd.ExcelWriter(xlsx, engine="openpyxl") as writer:
            df.to_excel(writer, index=False, sheet_name="Results_EOD")
            summary = pd.DataFrame([{
                "report_date": day,
                "scheduled_companies": len(rows),
                "qualified": sum(_safe_float(r.get("score")) >= DEFAULT_THRESHOLD and _safe_float(r.get("move_pct")) >= DEFAULT_MOVE for r in rows),
                "ntfy_sent": sum(str(r.get("ntfy_status")) == "NTFY_SENT" for r in rows),
                "today": sum(r.get("result_bucket") == "TODAY" for r in rows),
                "tomorrow": sum(r.get("result_bucket") == "TOMORROW" for r in rows),
            }])
            summary.to_excel(writer, index=False, sheet_name="Summary")
        print(f"[Results pre-event] EOD report: {xlsx} | companies={len(rows)}")
        return xlsx


def _wait_until(target: dt_time) -> None:
    now = datetime.now(TZ)
    target_dt = now.replace(hour=target.hour, minute=target.minute, second=0, microsecond=0)
    if now < target_dt:
        time.sleep((target_dt - now).total_seconds())


def main(argv: Optional[Sequence[str]] = None) -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--watch", action="store_true")
    p.add_argument("--once", action="store_true")
    p.add_argument("--lookahead-days", type=int, default=DEFAULT_LOOKAHEAD)
    p.add_argument("--output-dir", default=DEFAULT_OUTPUT)
    args = p.parse_args(argv)
    universe = load_afternoon_universe("Group_A.csv", "Group_B.csv", "bse_smallcap250_clean.csv")
    scanner = ResultsPreEventScanner(universe, args.output_dir, max(0, args.lookahead_days))
    now = datetime.now(TZ)
    if args.once:
        scanner.cycle(now)
        if now.time() >= EOD:
            scanner.write_report(now)
        return 0
    _wait_until(START)
    next_tick = datetime.now(TZ)
    while datetime.now(TZ).time() <= END:
        now = datetime.now(TZ)
        try:
            scanner.cycle(now)
        except Exception as exc:
            print(f"[Results pre-event] cycle failed; continuing: {type(exc).__name__}: {exc}")
        next_tick += timedelta(minutes=5)
        sleep_for = max(10.0, (next_tick - datetime.now(TZ)).total_seconds())
        time.sleep(sleep_for)
    _wait_until(EOD)
    scanner.write_report(datetime.now(TZ))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
