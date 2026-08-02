"""Command-line Early Swing Rally Scanner.

Example:
    python early_swing_scanner.py trading_toolkit.xlsx \
        bse1000_clean.csv Group_A.csv Group_B.csv

Ticker resolution for BSE records is deliberately multi-stage:
    1. BSE Security Id + .BO (ABB.BO)
    2. Security Id + .NS for dual-listed companies (ABB.NS)
    3. Numeric BSE Security Code + .BO (500002.BO)

This avoids the false "possibly delisted" flood caused by using numeric BSE codes
as the only Yahoo Finance ticker.
"""
from __future__ import annotations

import argparse
import hashlib
import logging
import math
import pickle
import time
from dataclasses import replace
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Dict, List, Optional, Sequence

import pandas as pd

from early_swing_config import DEFAULT_EARLY_SWING_CONFIG, EarlySwingConfig
from early_swing_alerts import generate_alerts
from early_swing_calibration import apply_calibration, load_calibration
from early_swing_engine import analyze_early_swing
from early_swing_features import classify_market_regime, normalise_ohlcv
from early_swing_models import EarlySwingCandidate, UniverseRecord
from early_swing_report import write_early_swing_report
from early_swing_universe import (
    load_and_deduplicate_universes,
    load_latest_bse_gainers_from_workbook,
    merge_universe_records,
)

LOGGER = logging.getLogger("early_swing")


def _get_yfinance():
    """Import yfinance lazily so offline unit tests can mock the provider."""
    import yfinance as yf
    return yf


def _quiet_yfinance_logs() -> None:
    """Suppress misleading per-ticker delisted messages.

    Missing histories are summarised once after each ticker strategy rather than
    printing hundreds of error lines from yfinance internals.
    """
    for logger_name in ("yfinance", "yfinance.scrapers", "yfinance.multi"):
        logging.getLogger(logger_name).setLevel(logging.CRITICAL)


def _cache_path(cache_dir: Path, stable_key: str) -> Path:
    digest = hashlib.sha1(stable_key.encode("utf-8")).hexdigest()[:16]
    return cache_dir / f"{digest}.pkl"


def _period_rank(period: str) -> int:
    """Approximate requested history length for safe cache compatibility."""
    text = str(period or "").strip().lower()
    if text == "max":
        return 10_000_000
    try:
        if text.endswith("y"):
            return int(text[:-1]) * 365
        if text.endswith("mo"):
            return int(text[:-2]) * 30
        if text.endswith("d"):
            return int(text[:-1])
    except ValueError:
        return 0
    return 0


def _load_cached(cache_dir: Path, record: UniverseRecord, max_age_hours: int,
                 requested_period: str) -> Optional[pd.DataFrame]:
    path = _cache_path(cache_dir, record.stable_key)
    if not path.exists():
        return None
    age = datetime.now() - datetime.fromtimestamp(path.stat().st_mtime)
    if age > timedelta(hours=max_age_hours):
        return None
    try:
        with path.open("rb") as handle:
            payload = pickle.load(handle)
        if isinstance(payload, dict) and "data" in payload:
            df = normalise_ohlcv(payload.get("data"))
            source_ticker = str(payload.get("source_ticker") or "")
            cached_period = str(payload.get("history_period") or "")
            # A one-year daily cache must never silently satisfy a five-year
            # calibration request. A longer cache may safely serve a shorter
            # live request.
            if cached_period and _period_rank(cached_period) < _period_rank(requested_period):
                return None
            if not cached_period and _period_rank(requested_period) > _period_rank("1y"):
                return None
        else:
            # Backward compatibility with the Phase-1 cache format.
            df = normalise_ohlcv(payload)
            source_ticker = ""
            if _period_rank(requested_period) > _period_rank("1y"):
                return None
        if not df.empty:
            df.attrs["source_ticker"] = source_ticker or record.preferred_ticker
            return df
    except Exception as exc:
        LOGGER.warning("Ignoring corrupt cache for %s: %s", record.stable_key, exc)
    return None


def _save_cache(cache_dir: Path, record: UniverseRecord, df: pd.DataFrame,
                history_period: str) -> None:
    cache_dir.mkdir(parents=True, exist_ok=True)
    path = _cache_path(cache_dir, record.stable_key)
    temp = path.with_suffix(".tmp")
    payload = {
        "stable_key": record.stable_key,
        "source_ticker": str(df.attrs.get("source_ticker") or record.preferred_ticker),
        "history_period": history_period,
        "data": df,
    }
    with temp.open("wb") as handle:
        pickle.dump(payload, handle, protocol=pickle.HIGHEST_PROTOCOL)
    temp.replace(path)


def _extract_from_batch(raw: pd.DataFrame, ticker: str) -> pd.DataFrame:
    if raw is None or raw.empty:
        return pd.DataFrame()
    if not isinstance(raw.columns, pd.MultiIndex):
        return normalise_ohlcv(raw)

    for level in range(raw.columns.nlevels):
        values = set(map(str, raw.columns.get_level_values(level)))
        if ticker not in values:
            continue
        try:
            frame = raw.xs(ticker, axis=1, level=level, drop_level=True)
            return normalise_ohlcv(frame)
        except (KeyError, ValueError):
            continue
    return pd.DataFrame()


def _download_strategy_pass(
    records: Sequence[UniverseRecord],
    candidate_index: int,
    config: EarlySwingConfig,
    pass_label: str,
) -> Dict[str, pd.DataFrame]:
    """Run one batch ticker strategy and return histories by stable record key."""
    yf = _get_yfinance()
    eligible = [record for record in records if len(record.ticker_candidates) > candidate_index]
    downloaded: Dict[str, pd.DataFrame] = {}
    if not eligible:
        return downloaded

    total_batches = math.ceil(len(eligible) / config.batch_size)
    for batch_number, start in enumerate(range(0, len(eligible), config.batch_size), start=1):
        chunk = list(eligible[start:start + config.batch_size])
        ticker_by_key = {
            record.stable_key: record.ticker_candidates[candidate_index]
            for record in chunk
        }
        tickers = list(dict.fromkeys(ticker_by_key.values()))
        LOGGER.info(
            "[%s %d/%d] requesting %d tickers (%s ... %s)",
            pass_label,
            batch_number,
            total_batches,
            len(tickers),
            tickers[0],
            tickers[-1],
        )

        raw = pd.DataFrame()
        for attempt in range(1, config.fetch_retries + 1):
            try:
                raw = yf.download(
                    tickers=tickers,
                    period=config.history_period,
                    interval="1d",
                    auto_adjust=True,
                    group_by="ticker",
                    threads=max(1, min(config.download_workers, len(tickers))),
                    progress=False,
                    timeout=config.fetch_timeout_seconds,
                )
                if raw is not None and not raw.empty:
                    break
            except Exception as exc:
                LOGGER.warning(
                    "[%s %d/%d] fetch attempt %d/%d failed: %s",
                    pass_label,
                    batch_number,
                    total_batches,
                    attempt,
                    config.fetch_retries,
                    exc,
                )
            if attempt < config.fetch_retries:
                time.sleep(attempt * 2)

        usable = 0
        for record in chunk:
            ticker = ticker_by_key[record.stable_key]
            frame = _extract_from_batch(raw, ticker)
            if not frame.empty and len(frame) >= config.minimum_history_sessions:
                frame.attrs["source_ticker"] = ticker
                downloaded[record.stable_key] = frame
                usable += 1

        LOGGER.info(
            "[%s %d/%d] %d/%d usable; %d unresolved",
            pass_label,
            batch_number,
            total_batches,
            usable,
            len(chunk),
            len(chunk) - usable,
        )
        if batch_number < total_batches and config.batch_pause_seconds > 0:
            time.sleep(config.batch_pause_seconds)

    return downloaded


def fetch_histories(
    records: Sequence[UniverseRecord],
    config: EarlySwingConfig,
    use_cache: bool = True,
) -> Dict[str, pd.DataFrame]:
    """Fetch histories with BSE-ID, NSE-ID, and numeric-code fallbacks.

    Results are keyed by ``UniverseRecord.stable_key`` rather than the attempted
    ticker, so a record remains addressable even when its successful source is an
    NSE fallback.
    """
    _quiet_yfinance_logs()
    cache_dir = Path(config.cache_directory)
    result: Dict[str, pd.DataFrame] = {}
    pending: List[UniverseRecord] = []

    for record in records:
        cached = _load_cached(cache_dir, record, config.cache_hours, config.history_period) if use_cache else None
        if cached is not None and not cached.empty:
            result[record.stable_key] = cached
        else:
            pending.append(record)

    LOGGER.info("History cache hits: %d | downloads required: %d", len(result), len(pending))
    if not pending:
        return result

    max_strategies = max((len(record.ticker_candidates) for record in pending), default=0)
    strategy_labels = ("BSE-ID", "NSE-ID", "BSE-CODE")

    unresolved = list(pending)
    record_by_key = {record.stable_key: record for record in records}
    for candidate_index in range(max_strategies):
        eligible = [record for record in unresolved if len(record.ticker_candidates) > candidate_index]
        if not eligible:
            continue
        pass_label = strategy_labels[candidate_index] if candidate_index < len(strategy_labels) else f"FALLBACK-{candidate_index + 1}"
        if candidate_index > 0:
            LOGGER.info("[fallback] Trying %s for %d unresolved securities", pass_label, len(eligible))
        pass_data = _download_strategy_pass(eligible, candidate_index, config, pass_label)
        result.update(pass_data)
        if use_cache:
            # Persist every successful strategy pass immediately. A long
            # five-year calibration run can therefore resume after an
            # interruption instead of losing all completed batches.
            for stable_key, frame in pass_data.items():
                record = record_by_key.get(stable_key)
                if record is not None:
                    _save_cache(cache_dir, record, frame, config.history_period)
        unresolved = [record for record in unresolved if record.stable_key not in result]
        if not unresolved:
            break

    for record in records:
        frame = result.get(record.stable_key)
        if frame is not None and not frame.empty and use_cache:
            _save_cache(cache_dir, record, frame, config.history_period)

    LOGGER.info(
        "Price-history resolution complete: %d/%d usable; %d unavailable across all ticker strategies",
        len(records) - len(unresolved),
        len(records),
        len(unresolved),
    )
    if unresolved:
        pd.DataFrame([
            {
                "symbol": record.symbol,
                "security_id": record.security_id,
                "company_name": record.company_name,
                "attempted_tickers": " | ".join(record.ticker_candidates),
                "reason": "NO_DATA_ACROSS_ALL_TICKER_STRATEGIES",
            }
            for record in unresolved
        ]).to_csv("early_swing_failed_symbols.csv", index=False)
    return result


def _fetch_benchmark(config: EarlySwingConfig) -> pd.DataFrame:
    _quiet_yfinance_logs()
    yf = _get_yfinance()
    raw = yf.download(
        tickers=config.benchmark_ticker,
        period=config.history_period,
        interval="1d",
        auto_adjust=True,
        progress=False,
        threads=False,
        timeout=config.fetch_timeout_seconds,
    )
    df = normalise_ohlcv(raw)
    if df.empty:
        raise RuntimeError(f"Could not fetch benchmark {config.benchmark_ticker}")
    return df


def run_scanner(
    xlsx_path: str,
    universe_paths: Sequence[str],
    config: EarlySwingConfig,
    max_symbols: int = 0,
    use_cache: bool = True,
    include_latest_gainers: bool = True,
    calibration_path: Optional[str] = None,
    alerts_enabled: Optional[bool] = None,
    alert_state_path: Optional[str] = None,
    alert_output_path: Optional[str] = None,
) -> List[EarlySwingCandidate]:
    file_records = load_and_deduplicate_universes(universe_paths)
    gainer_records = load_latest_bse_gainers_from_workbook(xlsx_path) if include_latest_gainers else []
    records = merge_universe_records([file_records, gainer_records])
    if gainer_records:
        LOGGER.info("Added %d securities from the latest BSE Gainer sheet before deduplication", len(gainer_records))
    if max_symbols > 0:
        records = records[:max_symbols]
        LOGGER.info("[test limit] Processing only the first %d securities", len(records))
    LOGGER.info("Loaded %d unique securities from %d universe file(s)", len(records), len(universe_paths))

    benchmark = _fetch_benchmark(config)
    regime = classify_market_regime(benchmark)
    histories = fetch_histories(records, config, use_cache=use_cache)

    candidates: List[EarlySwingCandidate] = []
    failures = []
    for idx, record in enumerate(records, 1):
        df = histories.get(record.stable_key, pd.DataFrame())
        if df.empty:
            failures.append({
                "symbol": record.symbol,
                "security_id": record.security_id,
                "attempted_tickers": " | ".join(record.ticker_candidates),
                "reason": "NO_DATA",
            })
            continue

        source_ticker = str(df.attrs.get("source_ticker") or record.preferred_ticker)
        resolved_record = replace(record, ticker=source_ticker)
        candidate = analyze_early_swing(resolved_record, df, benchmark, config, analysis_date=date.today())
        candidates.append(candidate)
        if idx % 50 == 0 or idx == len(records):
            LOGGER.info("Analysed %d/%d", idx, len(records))

    candidates.sort(key=lambda candidate: candidate.rules_score, reverse=True)

    calibration_file = calibration_path or config.calibration_file
    calibration = load_calibration(calibration_file)
    if calibration:
        apply_calibration(candidates, calibration, config)
        LOGGER.info(
            "Applied empirical calibration from %s (%s canonical trades)",
            calibration_file,
            calibration.get("selected_trade_count", 0),
        )
    else:
        LOGGER.info("Calibration file %s not available; probabilities remain NOT_AVAILABLE", calibration_file)

    should_generate_alerts = config.alert_enabled if alerts_enabled is None else alerts_enabled
    alerts = []
    if should_generate_alerts:
        alerts = generate_alerts(
            candidates,
            config,
            state_path=alert_state_path or config.alert_state_file,
            output_path=alert_output_path or config.alert_output_file,
        )
        LOGGER.info("Generated %d new or changed Early Swing alerts", len(alerts))

    write_early_swing_report(
        xlsx_path,
        candidates,
        regime,
        date.today(),
        alerts=alerts,
        calibration=calibration,
    )
    if failures:
        pd.DataFrame(failures).to_csv("early_swing_failed_symbols.csv", index=False)
    LOGGER.info("Report written to %s", xlsx_path)
    return candidates


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Early Swing Rally Scanner")
    parser.add_argument("workbook", help="Existing Excel workbook to update")
    parser.add_argument("universes", nargs="+", help="CSV universe files; optional LABEL=PATH form")
    parser.add_argument("--benchmark", default=DEFAULT_EARLY_SWING_CONFIG.benchmark_ticker)
    parser.add_argument("--top", type=int, default=DEFAULT_EARLY_SWING_CONFIG.maximum_actionable_results)
    parser.add_argument("--max-symbols", type=int, default=0, help="Testing cap; 0 means all")
    parser.add_argument("--batch-size", type=int, default=DEFAULT_EARLY_SWING_CONFIG.batch_size)
    parser.add_argument("--workers", type=int, default=DEFAULT_EARLY_SWING_CONFIG.download_workers)
    parser.add_argument("--no-cache", action="store_true")
    parser.add_argument("--no-gainers", action="store_true", help="Do not include the latest BSEGain_* workbook sheet")
    parser.add_argument("--calibration-file", default=DEFAULT_EARLY_SWING_CONFIG.calibration_file)
    parser.add_argument("--no-alerts", action="store_true")
    parser.add_argument("--alert-state-file", default=DEFAULT_EARLY_SWING_CONFIG.alert_state_file)
    parser.add_argument("--alert-output", default=DEFAULT_EARLY_SWING_CONFIG.alert_output_file)
    parser.add_argument("--alert-min-score", type=float, default=DEFAULT_EARLY_SWING_CONFIG.alert_minimum_rules_score)
    parser.add_argument("--alert-cooldown-days", type=int, default=DEFAULT_EARLY_SWING_CONFIG.alert_cooldown_days)
    parser.add_argument("--log-level", default="INFO", choices=["DEBUG", "INFO", "WARNING", "ERROR"])
    return parser


def main() -> None:
    args = build_parser().parse_args()
    logging.basicConfig(
        level=getattr(logging, args.log_level),
        format="%(asctime)s | %(levelname)s | %(message)s",
    )
    config = replace(
        DEFAULT_EARLY_SWING_CONFIG,
        benchmark_ticker=args.benchmark,
        maximum_actionable_results=args.top,
        batch_size=max(1, args.batch_size),
        download_workers=max(1, args.workers),
        alert_minimum_rules_score=max(0.0, min(100.0, args.alert_min_score)),
        alert_cooldown_days=max(0, args.alert_cooldown_days),
    )
    run_scanner(
        args.workbook,
        args.universes,
        config,
        max_symbols=args.max_symbols,
        use_cache=not args.no_cache,
        include_latest_gainers=not args.no_gainers,
        calibration_path=args.calibration_file,
        alerts_enabled=not args.no_alerts,
        alert_state_path=args.alert_state_file,
        alert_output_path=args.alert_output,
    )


if __name__ == "__main__":
    main()
