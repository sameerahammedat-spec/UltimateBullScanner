"""
Fast BSE Group A/B Scanner with Next-Day Explosive Move integration.

Why this version exists
-----------------------
BSE Group A/B CSV exports can contain more row fields than their header because
of trailing commas. pandas.read_csv() may then shift the first fields into a
MultiIndex. The old generic loader could accidentally select the ISIN (for
example INE117A01022) as the Yahoo symbol, leading to invalid tickers such as
INE117A01022.NS and INE117A01022.BO.

This scanner:
* parses the BSE CSVs with csv.reader and trims trailing empty fields safely;
* uses the BSE Security ID as the primary Yahoo ticker (for example ABB.BO);
* falls back to the NSE Security ID and finally the numeric BSE code only when needed;
* preserves actual company names and BSE Group A/B membership;
* downloads price history in bounded batches;
* calculates the first-stage technical scan locally (no Ticker.info request for
  every security);
* sends only the strongest configurable subset to the expensive Explosive Engine;
* never enters the market_scanner 90/180/360/... second global cooldown loop.

Existing command remains valid:
    python group_ab_scanner.py trading_toolkit.xlsx Group_A.csv Group_B.csv

Useful optional flags:
    --max-symbols 100       # quick test only
    --no-explosive          # run only the fast Group A/B table
    --batch-size 80
    --workers 8
    --explosive-limit 30
"""

from __future__ import annotations

import argparse
import csv
import math
import os
import logging
import pickle
import re
import time
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

import numpy as np
import pandas as pd
from openpyxl import load_workbook
from openpyxl.formatting.rule import CellIsRule
from openpyxl.styles import Alignment, Border, Font, PatternFill, Side
from openpyxl.utils import get_column_letter

# ---------------------------------------------------------------------------
# Visual style: matches the existing scanner workbook.
# ---------------------------------------------------------------------------
FONT = "Arial"
BLACK = Font(name=FONT, size=10)
BLACK_B = Font(name=FONT, size=10, bold=True)
HDR_FONT = Font(name=FONT, color="FFFFFF", size=10, bold=True)
HDR_FILL = PatternFill("solid", fgColor="1F3864")
SEC_FILL = PatternFill("solid", fgColor="548235")
SEC_FONT = Font(name=FONT, color="FFFFFF", size=11, bold=True)
ALT_FILL = PatternFill("solid", fgColor="F2F2F2")
GOOD_FILL = PatternFill("solid", fgColor="C6E0B4")
WATCH_FILL = PatternFill("solid", fgColor="FFE699")
AVOID_FILL = PatternFill("solid", fgColor="F8CBAD")
_THIN = Side(style="thin", color="BFBFBF")
BORDER = Border(left=_THIN, right=_THIN, top=_THIN, bottom=_THIN)

HEADERS = [
    "Symbol", "Name", "Sector", "Cap Segment", "Close", "% Chg", "Volume",
    "Vol Ratio (vs 3d avg)", "Mkt Cap (Cr)",
    "Liquidity OK?", "Above 50DMA?", "Above 200DMA?", "RSI(14)", "RSI Healthy?",
    "ATR(14)", "Vol Spike?", "% Down from 52wH", "Near 52wH?",
    "RS vs Sensex 1M %", "RS vs Sensex 3M %", "RS Positive?",
    "P/E", "ROE %", "Debt/Equity", "QoQ Profit Gr %",
    "Score (0-9)", "Verdict",
]

# ---------------------------------------------------------------------------
# Runtime defaults. All can be overridden by CLI flags or environment values.
# ---------------------------------------------------------------------------
DEFAULT_BATCH_SIZE = int(os.getenv("GROUP_AB_BATCH_SIZE", "80"))
DEFAULT_WORKERS = int(os.getenv("GROUP_AB_WORKERS", "8"))
DEFAULT_EXPLOSIVE_LIMIT = int(os.getenv("GROUP_AB_EXPLOSIVE_LIMIT", "30"))
DEFAULT_MAX_SYMBOLS = int(os.getenv("GROUP_AB_MAX_SYMBOLS", "0"))  # 0 = all
PRICE_PERIOD = os.getenv("GROUP_AB_PRICE_PERIOD", "1y")
BSE_BENCHMARK = os.getenv("GROUP_AB_BENCHMARK", "^BSESN")
MIN_LAST_VOLUME = int(os.getenv("GROUP_AB_MIN_LAST_VOLUME", "50000"))
MIN_MEDIAN_TURNOVER_CR = float(os.getenv("GROUP_AB_MIN_TURNOVER_CR", "0.25"))
CACHE_FILE = os.getenv("GROUP_AB_CACHE_FILE", "group_ab_price_cache.pkl")
CACHE_MAX_AGE_HOURS = float(os.getenv("GROUP_AB_CACHE_HOURS", "10"))

SYMBOL_ALIASES = (
    "security code", "scrip code", "bse code", "securitycode", "scripcode", "bsecode",
)
SECURITY_ID_ALIASES = (
    "security id", "securityid", "scrip id", "scripid", "ticker", "symbol",
)
NAME_ALIASES = (
    "issuer name", "issuername", "security name", "securityname", "company name",
    "companyname", "scrip name", "scripname", "name",
)
GROUP_ALIASES = ("group", "bse group", "bsegroup")
SECTOR_ALIASES = (
    "sector", "industry", "industry name", "industryname", "macro-economic sector",
    "macroeconomicsector",
)


@dataclass(frozen=True)
class UniverseRecord:
    symbol: str             # six-digit BSE Security Code
    security_id: str        # human-friendly BSE Security ID, e.g. ABB
    name: str
    sector: str
    group: str
    sources: Tuple[str, ...]

    @property
    def normalized_security_id(self) -> str:
        """Return a Yahoo-compatible BSE/NSE symbol base from Security Id."""
        value = str(self.security_id or "").strip().upper()
        value = re.sub(r"\.(BO|NS)$", "", value, flags=re.IGNORECASE)
        # BSE Security Id values normally contain letters, digits, &, and hyphens.
        # Remove whitespace/other punctuation but preserve & and - because Yahoo uses
        # them for some Indian tickers.
        return re.sub(r"[^A-Z0-9&-]+", "", value)

    @property
    def yahoo_ticker(self) -> str:
        """Primary ticker: BSE Security Id + .BO, not the numeric scrip code."""
        base = self.normalized_security_id
        return f"{base}.BO" if base else f"{self.symbol}.BO"

    @property
    def yahoo_fallback_tickers(self) -> Tuple[str, ...]:
        """Fallbacks tried only if the primary BSE Security Id ticker has no data."""
        candidates: List[str] = []
        base = self.normalized_security_id
        if base:
            # Many Group A/B companies are dual-listed; NSE is a useful fallback
            # when Yahoo has no BSE history for the same company.
            candidates.append(f"{base}.NS")
        candidates.append(f"{self.symbol}.BO")
        return tuple(t for t in candidates if t != self.yahoo_ticker)

    @property
    def cap_segment(self) -> str:
        return f"BSE Group {self.group}" if self.group else "BSE Group A/B"


# ---------------------------------------------------------------------------
# Robust BSE CSV loading
# ---------------------------------------------------------------------------
def _norm(value: Any) -> str:
    return re.sub(r"[^a-z0-9]+", "", str(value or "").strip().lower())


def _clean(value: Any) -> str:
    text = str(value or "").strip()
    return "" if text.lower() in {"", "nan", "none", "null", "n/a", "na", "-", "--"} else text


def _find_header(header: Sequence[str], aliases: Sequence[str]) -> Optional[int]:
    normalized = [_norm(column) for column in header]
    for alias in aliases:
        target = _norm(alias)
        if target in normalized:
            return normalized.index(target)
    return None


def _read_csv_rows(path: Path) -> Tuple[List[str], List[List[str]]]:
    last_error: Optional[Exception] = None
    for encoding in ("utf-8-sig", "utf-8", "latin-1"):
        try:
            with path.open("r", encoding=encoding, newline="") as handle:
                reader = csv.reader(handle)
                all_rows = list(reader)
            if not all_rows:
                raise ValueError(f"{path} is empty")
            header = [str(value).strip() for value in all_rows[0]]
            rows: List[List[str]] = []
            for raw in all_rows[1:]:
                row = list(raw)
                # BSE exports often append 3-4 empty fields beyond the header.
                while len(row) > len(header) and row and not str(row[-1]).strip():
                    row.pop()
                if len(row) > len(header):
                    # Preserve the actual leading columns and ignore unexplained
                    # extras only after reporting them through a deterministic trim.
                    row = row[: len(header)]
                elif len(row) < len(header):
                    row.extend([""] * (len(header) - len(row)))
                rows.append(row)
            return header, rows
        except UnicodeError as exc:
            last_error = exc
    raise ValueError(f"Unable to read {path}: {last_error}")


def load_group_file(path_text: str, expected_group: str) -> Dict[str, UniverseRecord]:
    path = Path(path_text)
    header, rows = _read_csv_rows(path)

    symbol_idx = _find_header(header, SYMBOL_ALIASES)
    security_id_idx = _find_header(header, SECURITY_ID_ALIASES)
    name_idx = _find_header(header, NAME_ALIASES)
    group_idx = _find_header(header, GROUP_ALIASES)
    sector_idx = _find_header(header, SECTOR_ALIASES)

    if symbol_idx is None:
        raise ValueError(
            f"{path}: no BSE Security Code/Scrip Code column found. Columns: {header}"
        )

    records: Dict[str, UniverseRecord] = {}
    invalid_codes = 0

    for row in rows:
        symbol = _clean(row[symbol_idx])
        if re.fullmatch(r"\d+\.0+", symbol):
            symbol = symbol.split(".", 1)[0]
        symbol = re.sub(r"\D", "", symbol)
        if not re.fullmatch(r"\d{6}", symbol):
            invalid_codes += 1
            continue

        security_id = _clean(row[security_id_idx]) if security_id_idx is not None else ""
        name = _clean(row[name_idx]) if name_idx is not None else ""
        group = (_clean(row[group_idx]) if group_idx is not None else expected_group).strip().upper()
        group = group[:1] if group else expected_group.upper()
        sector = _clean(row[sector_idx]) if sector_idx is not None else ""

        if not name or name.isdigit() or name.upper().startswith("INE"):
            name = security_id or f"Name unavailable ({symbol})"

        records[symbol] = UniverseRecord(
            symbol=symbol,
            security_id=security_id,
            name=name,
            sector=sector,
            group=group,
            sources=(f"Group {expected_group.upper()}",),
        )

    print(
        f"[Group {expected_group.upper()} loader] {path.name}: "
        f"{len(records)} valid BSE codes, {invalid_codes} invalid rows skipped; "
        f"using Security Id -> <security-id>.BO (with NSE/numeric fallbacks)"
    )
    return records


def load_group_ab_universe(group_a_path: str, group_b_path: str) -> List[UniverseRecord]:
    group_a = load_group_file(group_a_path, "A")
    group_b = load_group_file(group_b_path, "B")

    merged: Dict[str, UniverseRecord] = dict(group_a)
    duplicate_count = 0
    for symbol, candidate in group_b.items():
        if symbol not in merged:
            merged[symbol] = candidate
            continue
        duplicate_count += 1
        current = merged[symbol]
        merged[symbol] = UniverseRecord(
            symbol=symbol,
            security_id=current.security_id or candidate.security_id,
            name=current.name if not current.name.startswith("Name unavailable") else candidate.name,
            sector=current.sector or candidate.sector,
            group=current.group or candidate.group,
            sources=tuple(sorted(set(current.sources + candidate.sources))),
        )

    print(
        f"Group A: {len(group_a)} | Group B: {len(group_b)} | "
        f"Unique combined: {len(merged)} | duplicates removed: {duplicate_count}"
    )
    return list(merged.values())


# ---------------------------------------------------------------------------
# Indicator calculations
# ---------------------------------------------------------------------------
def _to_float(value: Any) -> Optional[float]:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return None if not math.isfinite(number) else number


def rsi(series: pd.Series, period: int = 14) -> pd.Series:
    delta = series.diff()
    gain = delta.clip(lower=0)
    loss = -delta.clip(upper=0)
    avg_gain = gain.rolling(period).mean()
    avg_loss = loss.rolling(period).mean()
    rs = avg_gain / avg_loss.replace(0, np.nan)
    return 100 - (100 / (1 + rs))


def atr(df: pd.DataFrame, period: int = 14) -> pd.Series:
    high, low, close = df["High"], df["Low"], df["Close"]
    prev_close = close.shift(1)
    tr = pd.concat(
        [(high - low), (high - prev_close).abs(), (low - prev_close).abs()], axis=1
    ).max(axis=1)
    return tr.rolling(period).mean()


def adx(df: pd.DataFrame, period: int = 14) -> pd.Series:
    high, low, close = df["High"], df["Low"], df["Close"]
    up_move = high.diff()
    down_move = -low.diff()
    plus_dm = pd.Series(
        np.where((up_move > down_move) & (up_move > 0), up_move, 0.0), index=df.index
    )
    minus_dm = pd.Series(
        np.where((down_move > up_move) & (down_move > 0), down_move, 0.0), index=df.index
    )
    tr = pd.concat(
        [(high - low), (high - close.shift(1)).abs(), (low - close.shift(1)).abs()], axis=1
    ).max(axis=1)
    atr_s = tr.rolling(period).mean().replace(0, np.nan)
    plus_di = 100 * plus_dm.rolling(period).mean() / atr_s
    minus_di = 100 * minus_dm.rolling(period).mean() / atr_s
    denominator = (plus_di + minus_di).replace(0, np.nan)
    dx = 100 * (plus_di - minus_di).abs() / denominator
    return dx.rolling(period).mean()


def trend_regression(close: pd.Series, window: int = 20) -> Tuple[Optional[float], Optional[float]]:
    if len(close) < window:
        return None, None
    values = close.iloc[-window:].astype(float).to_numpy()
    if np.any(~np.isfinite(values)) or np.any(values <= 0):
        return None, None
    y = np.log(values)
    x = np.arange(window, dtype=float)
    slope, intercept = np.polyfit(x, y, 1)
    y_hat = slope * x + intercept
    ss_res = float(np.sum((y - y_hat) ** 2))
    ss_tot = float(np.sum((y - np.mean(y)) ** 2))
    r2 = 1 - ss_res / ss_tot if ss_tot > 0 else 0.0
    return float((np.exp(slope) - 1) * 100), float(r2)


# ---------------------------------------------------------------------------
# Efficient price download
# ---------------------------------------------------------------------------
def _load_cache() -> Dict[str, Any]:
    try:
        with open(CACHE_FILE, "rb") as handle:
            payload = pickle.load(handle)
        created = payload.get("created")
        if not isinstance(created, datetime):
            return {}
        if datetime.now() - created > timedelta(hours=CACHE_MAX_AGE_HOURS):
            return {}
        data = payload.get("data", {})
        return data if isinstance(data, dict) else {}
    except (FileNotFoundError, OSError, pickle.PickleError, EOFError, AttributeError):
        return {}


def _save_cache(data: Dict[str, pd.DataFrame]) -> None:
    try:
        temporary = f"{CACHE_FILE}.tmp"
        with open(temporary, "wb") as handle:
            pickle.dump({"created": datetime.now(), "data": data}, handle, protocol=pickle.HIGHEST_PROTOCOL)
        os.replace(temporary, CACHE_FILE)
    except OSError as exc:
        print(f"[cache] Could not save {CACHE_FILE}: {exc}")


def _normalize_ohlcv(frame: pd.DataFrame) -> Optional[pd.DataFrame]:
    if frame is None or frame.empty:
        return None
    out = frame.copy()
    required = ["Open", "High", "Low", "Close", "Volume"]
    if isinstance(out.columns, pd.MultiIndex):
        flattened = False
        for level in range(out.columns.nlevels):
            level_values = [str(value) for value in out.columns.get_level_values(level)]
            if set(required).issubset(set(level_values)):
                out.columns = level_values
                flattened = True
                break
        if not flattened:
            return None
    missing = [column for column in required if column not in out.columns]
    if missing:
        return None
    out = out[required].apply(pd.to_numeric, errors="coerce")
    out = out.dropna(subset=["High", "Low", "Close"])
    if out.empty:
        return None
    out["Volume"] = out["Volume"].fillna(0)
    if getattr(out.index, "tz", None) is not None:
        out.index = out.index.tz_localize(None)
    return out


def _extract_ticker_frame(batch: pd.DataFrame, ticker: str) -> Optional[pd.DataFrame]:
    if batch is None or batch.empty:
        return None
    if not isinstance(batch.columns, pd.MultiIndex):
        return _normalize_ohlcv(batch)

    # group_by='ticker' normally places ticker at level 0.
    for level in range(batch.columns.nlevels):
        values = set(str(value) for value in batch.columns.get_level_values(level))
        if ticker not in values:
            continue
        try:
            frame = batch.xs(ticker, axis=1, level=level, drop_level=True)
            return _normalize_ohlcv(frame)
        except (KeyError, ValueError):
            continue
    return None


def _quiet_yfinance_logs() -> None:
    """Suppress yfinance's misleading per-ticker 'possibly delisted' spam.

    Missing symbols are summarized by this scanner after each pass instead.
    """
    for logger_name in ("yfinance", "yfinance.scrapers", "yfinance.multi"):
        logging.getLogger(logger_name).setLevel(logging.CRITICAL)


def _download_ticker_pass(
    records: Sequence[UniverseRecord],
    ticker_for_record,
    *,
    period: str,
    batch_size: int,
    workers: int,
    pass_label: str,
) -> Dict[str, pd.DataFrame]:
    """Download one ticker strategy and return histories keyed by BSE code."""
    import yfinance as yf

    downloaded: Dict[str, pd.DataFrame] = {}
    if not records:
        return downloaded

    total_batches = math.ceil(len(records) / batch_size)
    for batch_number, start in enumerate(range(0, len(records), batch_size), start=1):
        chunk = list(records[start : start + batch_size])
        ticker_map = {record.symbol: ticker_for_record(record) for record in chunk}
        tickers = list(dict.fromkeys(ticker_map.values()))
        print(
            f"[{pass_label} {batch_number}/{total_batches}] requesting {len(tickers)} tickers "
            f"({tickers[0]} ... {tickers[-1]})"
        )

        batch_data: Optional[pd.DataFrame] = None
        for attempt in range(1, 3):
            try:
                batch_data = yf.download(
                    tickers=tickers,
                    period=period,
                    interval="1d",
                    auto_adjust=True,
                    group_by="ticker",
                    threads=max(1, workers),
                    progress=False,
                    timeout=25,
                )
                break
            except Exception as exc:
                wait_seconds = 5 * attempt
                print(
                    f"[{pass_label} {batch_number}] attempt {attempt}/2 failed: {exc}. "
                    f"Retrying in {wait_seconds}s..."
                )
                time.sleep(wait_seconds)

        usable = 0
        if batch_data is not None and not batch_data.empty:
            for record in chunk:
                ticker = ticker_map[record.symbol]
                frame = _extract_ticker_frame(batch_data, ticker)
                if frame is not None and len(frame) >= 60:
                    frame.attrs["source_ticker"] = ticker
                    downloaded[record.symbol] = frame
                    usable += 1

        missing = len(chunk) - usable
        print(f"[{pass_label} {batch_number}/{total_batches}] {usable}/{len(chunk)} usable; {missing} unresolved")
        if batch_number < total_batches:
            time.sleep(0.5)

    return downloaded


def download_histories(
    records: Sequence[UniverseRecord],
    period: str,
    batch_size: int,
    workers: int,
    use_cache: bool = True,
) -> Dict[str, pd.DataFrame]:
    """Fetch histories with a high-coverage, low-noise ticker strategy.

    Order:
      1. BSE Security Id + .BO (e.g. AEGISLOG.BO)
      2. Security Id + .NS for dual-listed companies
      3. Numeric BSE code + .BO as a final fallback

    The prior version used numeric codes first. Yahoo exposes many BSE companies
    under their Security Id, so numeric-first produced dozens of false 'delisted'
    failures.
    """
    _quiet_yfinance_logs()

    histories: Dict[str, pd.DataFrame] = _load_cache() if use_cache and period == PRICE_PERIOD else {}
    pending = [record for record in records if record.symbol not in histories]

    if histories:
        print(f"[cache] Reusing {len(histories)} cached Group A/B histories")
    if not pending:
        return histories

    # Primary: actual BSE Security Id, the symbol Yahoo normally exposes.
    primary = _download_ticker_pass(
        pending,
        lambda record: record.yahoo_ticker,
        period=period, batch_size=batch_size, workers=workers,
        pass_label="BSE-ID",
    )
    histories.update(primary)

    unresolved = [record for record in pending if record.symbol not in histories]

    # First fallback: NSE Security Id for dual-listed companies. This is only used
    # when Yahoo lacks the BSE history and keeps the scan useful without stalling.
    nse_candidates = [record for record in unresolved if record.normalized_security_id]
    if nse_candidates:
        print(f"[fallback] Trying NSE Security Id for {len(nse_candidates)} unresolved companies")
        nse_data = _download_ticker_pass(
            nse_candidates,
            lambda record: f"{record.normalized_security_id}.NS",
            period=period, batch_size=batch_size, workers=workers,
            pass_label="NSE-ID",
        )
        histories.update(nse_data)

    unresolved = [record for record in unresolved if record.symbol not in histories]

    # Final fallback: legacy numeric BSE code. Some Yahoo listings do use this.
    if unresolved:
        print(f"[fallback] Trying numeric BSE codes for {len(unresolved)} unresolved companies")
        numeric_data = _download_ticker_pass(
            unresolved,
            lambda record: f"{record.symbol}.BO",
            period=period, batch_size=batch_size, workers=workers,
            pass_label="BSE-CODE",
        )
        histories.update(numeric_data)

    unresolved_count = sum(1 for record in records if record.symbol not in histories)
    print(
        f"Price-history resolution complete: {len(records) - unresolved_count}/{len(records)} usable; "
        f"{unresolved_count} unavailable across all ticker strategies"
    )

    if use_cache and period == PRICE_PERIOD:
        _save_cache(histories)
    return histories


def fetch_benchmark(period: str = "1y") -> Tuple[pd.DataFrame, Optional[float], Optional[float]]:
    import yfinance as yf

    data = yf.download(
        tickers=BSE_BENCHMARK,
        period=period,
        interval="1d",
        auto_adjust=True,
        progress=False,
        threads=False,
        timeout=25,
    )
    frame = _normalize_ohlcv(data)
    if frame is None:
        raise RuntimeError(f"Unable to fetch benchmark {BSE_BENCHMARK}")
    close = frame["Close"]
    ret_1m = (float(close.iloc[-1] / close.iloc[-22] - 1) * 100) if len(close) > 22 else None
    ret_3m = (float(close.iloc[-1] / close.iloc[-63] - 1) * 100) if len(close) > 63 else None
    return frame, ret_1m, ret_3m


# ---------------------------------------------------------------------------
# Local technical scan (no slow per-symbol fundamental metadata calls)
# ---------------------------------------------------------------------------
def analyze_history(
    record: UniverseRecord,
    df: pd.DataFrame,
    benchmark_1m: Optional[float],
    benchmark_3m: Optional[float],
) -> Optional[Dict[str, Any]]:
    if df is None or len(df) < 60:
        return None

    close = df["Close"]
    volume = df["Volume"]
    last_close = _to_float(close.iloc[-1])
    prev_close = _to_float(close.iloc[-2]) if len(close) > 1 else None
    last_volume = _to_float(volume.iloc[-1])
    if last_close is None or prev_close is None or last_volume is None or prev_close <= 0:
        return None

    pct_change = (last_close / prev_close - 1) * 100
    dma50 = _to_float(close.rolling(50).mean().iloc[-1])
    dma200 = _to_float(close.rolling(200).mean().iloc[-1]) if len(close) >= 200 else None
    rsi14 = _to_float(rsi(close, 14).iloc[-1])
    atr14 = _to_float(atr(df, 14).iloc[-1])
    adx14 = _to_float(adx(df, 14).iloc[-1])
    slope_pct, trend_r2 = trend_regression(close, 20)
    trend_quality = adx14 * trend_r2 if adx14 is not None and trend_r2 is not None else None

    avg_vol20 = _to_float(volume.rolling(20).mean().iloc[-1])
    median_turnover_cr = _to_float((close * volume).rolling(20).median().iloc[-1] / 1e7)
    vol_ratio20 = last_volume / avg_vol20 if avg_vol20 and avg_vol20 > 0 else None
    prior3 = _to_float(volume.iloc[-4:-1].mean()) if len(volume) >= 4 else None
    vol_ratio3 = last_volume / prior3 if prior3 and prior3 > 0 else None
    vol_spike = bool(vol_ratio20 is not None and vol_ratio20 >= 1.5)

    high52 = _to_float(close.rolling(min(252, len(close))).max().iloc[-1])
    down_from_high = ((high52 - last_close) / high52 * 100) if high52 and high52 > 0 else None

    stock1m = (last_close / float(close.iloc[-22]) - 1) * 100 if len(close) > 22 and close.iloc[-22] else None
    stock3m = (last_close / float(close.iloc[-63]) - 1) * 100 if len(close) > 63 and close.iloc[-63] else None
    rs1m = stock1m - benchmark_1m if stock1m is not None and benchmark_1m is not None else None
    rs3m = stock3m - benchmark_3m if stock3m is not None and benchmark_3m is not None else None

    liquidity_ok = bool(
        last_volume >= MIN_LAST_VOLUME
        and median_turnover_cr is not None
        and median_turnover_cr >= MIN_MEDIAN_TURNOVER_CR
    )
    above50 = bool(dma50 is not None and last_close > dma50)
    above200 = bool(dma200 is not None and last_close > dma200) if dma200 is not None else None
    rsi_healthy = bool(rsi14 is not None and 50 <= rsi14 <= 70)
    near_high = bool(down_from_high is not None and down_from_high <= 10)
    rs_positive = bool(rs3m is not None and rs3m > 0)

    # Technical 0-9 score; avoids 2,311 slow Ticker.info/financial statement calls.
    checks = [
        liquidity_ok,
        above50,
        bool(above200),
        rsi_healthy,
        vol_spike,
        near_high,
        rs_positive,
        bool(trend_quality is not None and trend_quality >= 10),
        bool(median_turnover_cr is not None and median_turnover_cr >= 1.0),
    ]
    score = sum(1 for item in checks if item)
    verdict_text = "STRONG SETUP" if score >= 7 else "WATCHLIST" if score >= 5 else "AVOID"

    return {
        "symbol": record.symbol,
        "security_id": record.security_id,
        "name": record.name,
        "sector": record.sector,
        "cap_segment": record.cap_segment,
        "scanner_sources": list(record.sources),
        "ticker": str(df.attrs.get("source_ticker") or record.yahoo_ticker),
        "data_exchange": ("NSE fallback" if str(df.attrs.get("source_ticker", "")).endswith(".NS") else "BSE"),
        "close": last_close,
        "pct_change": pct_change,
        "volume": int(last_volume),
        "vol_ratio_3d": vol_ratio3,
        "vol_ratio_20d": vol_ratio20,
        "mkt_cap_cr": None,
        "median_turnover_cr": median_turnover_cr,
        "liquidity_ok": liquidity_ok,
        "above_50dma": above50,
        "above_200dma": above200,
        "rsi": rsi14,
        "rsi_healthy": rsi_healthy,
        "atr": atr14,
        "adx": adx14,
        "trend_slope_pct": slope_pct,
        "trend_r2": trend_r2,
        "trend_quality": trend_quality,
        "vol_spike": vol_spike,
        "down_from_high": down_from_high,
        "near_high": near_high,
        "rs_1m": rs1m,
        "rs_3m": rs3m,
        "rs_positive": rs_positive,
        # Fundamentals intentionally unavailable in this fast first stage.
        "pe": None,
        "roe": None,
        "debt_equity": None,
        "qoq_growth": None,
        "score": score,
        "verdict": verdict_text,
    }


# ---------------------------------------------------------------------------
# Explosive engine integration: only top shortlist receives multi-year data.
# ---------------------------------------------------------------------------
def run_explosive_analysis(
    ranked_rows: Sequence[Dict[str, Any]],
    records_by_symbol: Dict[str, UniverseRecord],
    benchmark_frame: pd.DataFrame,
    benchmark_1m: Optional[float],
    benchmark_3m: Optional[float],
    batch_size: int,
    workers: int,
    limit: int,
) -> Tuple[List[Any], List[Any], Any]:
    try:
        from explosive_config import DEFAULT_CONFIG
        from explosive_engine import analyze_symbol_explosive
        from explosive_market_regime import classify_regime
    except Exception as exc:
        print(f"[explosive] Disabled because imports failed: {exc}")
        return [], [], None

    shortlist = list(ranked_rows[: max(0, limit)])
    if not shortlist:
        return [], [], None

    shortlist_records = [records_by_symbol[row["symbol"]] for row in shortlist]
    multi_year = download_histories(
        shortlist_records,
        period=f"{DEFAULT_CONFIG.calibration_years}y",
        batch_size=min(batch_size, max(1, limit)),
        workers=workers,
        use_cache=False,
    )

    above50_flags = [bool(row.get("above_50dma")) for row in ranked_rows]
    regime = classify_regime(benchmark_frame["Close"], above50_flags)

    candidates: List[Any] = []
    rejected: List[Any] = []
    for row in shortlist:
        symbol = row["symbol"]
        history = multi_year.get(symbol)
        if history is None or history.empty:
            continue
        try:
            candidate = analyze_symbol_explosive(
                symbol=symbol,
                name=row["name"],
                exchange="BSE",
                scanner_sources=row.get("scanner_sources", []),
                sector=row.get("sector", ""),
                df=history,
                nifty_1m=benchmark_1m,
                nifty_3m=benchmark_3m,
                market_regime=regime,
                config=DEFAULT_CONFIG,
                analysis_date=date.today(),
                run_calibration=True,
                is_smallcap=(row.get("cap_segment") == "BSE Group B"),
                min_traded_value_cr=MIN_MEDIAN_TURNOVER_CR,
            )
        except Exception as exc:
            print(f"[explosive] {symbol}: analysis failed: {exc}")
            continue
        (rejected if candidate.rejected else candidates).append(candidate)

    candidates.sort(key=lambda item: item.final_score, reverse=True)
    return candidates, rejected, regime


# ---------------------------------------------------------------------------
# Excel output
# ---------------------------------------------------------------------------
def _yn(value: Any) -> str:
    return "N/A" if value is None else "Y" if value else "N"


def _num(value: Any, decimals: int = 1) -> Any:
    number = _to_float(value)
    return round(number, decimals) if number is not None else "N/A"


def write_table(ws, start_row: int, rows: Sequence[Dict[str, Any]], title: str) -> int:
    row_number = start_row
    ws.cell(row=row_number, column=1, value=title).font = SEC_FONT
    for column in range(1, len(HEADERS) + 1):
        ws.cell(row=row_number, column=column).fill = SEC_FILL
    ws.merge_cells(start_row=row_number, start_column=1, end_row=row_number, end_column=len(HEADERS))
    row_number += 1

    for column, header in enumerate(HEADERS, start=1):
        cell = ws.cell(row=row_number, column=column, value=header)
        cell.font = HDR_FONT
        cell.fill = HDR_FILL
        cell.alignment = Alignment(horizontal="center", vertical="center", wrap_text=True)
        cell.border = BORDER
    ws.row_dimensions[row_number].height = 32
    header_row = row_number
    row_number += 1

    for index, row in enumerate(rows):
        values = [
            row["symbol"], row["name"], row.get("sector", ""), row.get("cap_segment", ""),
            _num(row.get("close"), 2), _num(row.get("pct_change"), 2), row.get("volume", "N/A"),
            _num(row.get("vol_ratio_3d"), 2), _num(row.get("mkt_cap_cr"), 0),
            _yn(row.get("liquidity_ok")), _yn(row.get("above_50dma")), _yn(row.get("above_200dma")),
            _num(row.get("rsi"), 1), _yn(row.get("rsi_healthy")), _num(row.get("atr"), 2),
            _yn(row.get("vol_spike")), _num(row.get("down_from_high"), 1), _yn(row.get("near_high")),
            _num(row.get("rs_1m"), 1), _num(row.get("rs_3m"), 1), _yn(row.get("rs_positive")),
            _num(row.get("pe"), 1), _num(row.get("roe"), 1), _num(row.get("debt_equity"), 2),
            _num(row.get("qoq_growth"), 1), row.get("score"), row.get("verdict"),
        ]
        for column, value in enumerate(values, start=1):
            cell = ws.cell(row=row_number, column=column, value=value)
            cell.font = BLACK_B if column == len(HEADERS) else BLACK
            cell.border = BORDER
            cell.alignment = Alignment(horizontal="center", vertical="center", wrap_text=(column in (2, 3)))
            if index % 2 == 1:
                cell.fill = ALT_FILL
        row_number += 1

    last_row = row_number - 1
    verdict_column = get_column_letter(len(HEADERS))
    if last_row > header_row:
        ws.conditional_formatting.add(
            f"{verdict_column}{header_row + 1}:{verdict_column}{last_row}",
            CellIsRule(operator="equal", formula=['"STRONG SETUP"'], fill=GOOD_FILL),
        )
        ws.conditional_formatting.add(
            f"{verdict_column}{header_row + 1}:{verdict_column}{last_row}",
            CellIsRule(operator="equal", formula=['"WATCHLIST"'], fill=WATCH_FILL),
        )
        ws.conditional_formatting.add(
            f"{verdict_column}{header_row + 1}:{verdict_column}{last_row}",
            CellIsRule(operator="equal", formula=['"AVOID"'], fill=AVOID_FILL),
        )
    return row_number + 1


def write_workbook(
    xlsx_path: str,
    full_scan: Sequence[Dict[str, Any]],
    explosive_candidates: Sequence[Any],
    explosive_rejected: Sequence[Any],
    regime: Any,
) -> str:
    today = date.today()
    sheet_name = f"GroupAB_{today.strftime('%d%b%y')}"
    workbook = load_workbook(xlsx_path)
    if sheet_name in workbook.sheetnames:
        del workbook[sheet_name]
    ws = workbook.create_sheet(sheet_name)
    ws.sheet_view.showGridLines = False
    ws.cell(
        row=1,
        column=1,
        value=f"BSE Group A/B Fast Scan — {today.strftime('%d %b %Y')}",
    ).font = Font(name=FONT, size=14, bold=True, color="1F3864")
    ws.merge_cells(start_row=1, start_column=1, end_row=1, end_column=len(HEADERS))

    next_row = write_table(
        ws,
        3,
        full_scan,
        title=f"FULL TECHNICAL SCAN (BSE Group A + B, deduplicated) — {len(full_scan)} liquid stocks",
    )

    try:
        from explosive_report import write_explosive_section

        write_explosive_section(
            ws,
            next_row,
            list(explosive_candidates),
            list(explosive_rejected),
            [],
            {
                "Qualifying explosive setups": len(explosive_candidates),
                "Rejected explosive candidates": len(explosive_rejected),
                "Market regime": str(regime) if regime is not None else "N/A",
            },
            today.strftime("%d %b %Y"),
        )
    except Exception as exc:
        ws.cell(row=next_row, column=1, value=f"Explosive report unavailable: {exc}").font = SEC_FONT
        print(f"[explosive report] Could not write section: {exc}")

    for column in range(1, len(HEADERS) + 1):
        ws.column_dimensions[get_column_letter(column)].width = 14
    ws.column_dimensions["B"].width = 34
    ws.column_dimensions["C"].width = 22
    ws.freeze_panes = "E5"
    workbook.save(xlsx_path)
    return sheet_name


# ---------------------------------------------------------------------------
# Orchestration and CLI
# ---------------------------------------------------------------------------
def run_group_ab_scan(
    group_a_path: str,
    group_b_path: str,
    *,
    max_symbols: int = 0,
    batch_size: int = DEFAULT_BATCH_SIZE,
    workers: int = DEFAULT_WORKERS,
    explosive_limit: int = DEFAULT_EXPLOSIVE_LIMIT,
    run_explosive: bool = True,
    use_cache: bool = True,
) -> Tuple[List[Dict[str, Any]], List[Any], List[Any], Any]:
    universe = load_group_ab_universe(group_a_path, group_b_path)
    if max_symbols > 0:
        universe = universe[:max_symbols]
        print(f"[test limit] Processing only the first {len(universe)} securities")

    records_by_symbol = {record.symbol: record for record in universe}
    benchmark_frame, benchmark_1m, benchmark_3m = fetch_benchmark("1y")
    print(
        f"Sensex benchmark: 1M={benchmark_1m:.2f}% | 3M={benchmark_3m:.2f}%"
        if benchmark_1m is not None and benchmark_3m is not None
        else "Sensex benchmark loaded"
    )

    histories = download_histories(
        universe,
        period=PRICE_PERIOD,
        batch_size=batch_size,
        workers=workers,
        use_cache=use_cache,
    )

    rows: List[Dict[str, Any]] = []
    for record in universe:
        analyzed = analyze_history(record, histories.get(record.symbol), benchmark_1m, benchmark_3m)
        if analyzed is not None and analyzed["liquidity_ok"]:
            rows.append(analyzed)

    rows.sort(
        key=lambda row: (
            row.get("score", -1),
            row.get("rs_3m") if row.get("rs_3m") is not None else -999,
            row.get("vol_ratio_20d") if row.get("vol_ratio_20d") is not None else -999,
        ),
        reverse=True,
    )
    print(f"Fast technical stage complete: {len(rows)} liquid/usable stocks")

    explosive_candidates: List[Any] = []
    explosive_rejected: List[Any] = []
    regime = None
    if run_explosive:
        explosive_candidates, explosive_rejected, regime = run_explosive_analysis(
            rows,
            records_by_symbol,
            benchmark_frame,
            benchmark_1m,
            benchmark_3m,
            batch_size=batch_size,
            workers=workers,
            limit=explosive_limit,
        )
        print(
            f"Explosive stage complete: {len(explosive_candidates)} qualifying, "
            f"{len(explosive_rejected)} rejected"
        )

    return rows, explosive_candidates, explosive_rejected, regime


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Fast BSE Group A/B scanner")
    parser.add_argument("xlsx_path")
    parser.add_argument("group_a_csv")
    parser.add_argument("group_b_csv")
    parser.add_argument("--max-symbols", type=int, default=DEFAULT_MAX_SYMBOLS)
    parser.add_argument("--batch-size", type=int, default=DEFAULT_BATCH_SIZE)
    parser.add_argument("--workers", type=int, default=DEFAULT_WORKERS)
    parser.add_argument("--explosive-limit", type=int, default=DEFAULT_EXPLOSIVE_LIMIT)
    parser.add_argument("--no-explosive", action="store_true")
    parser.add_argument("--no-cache", action="store_true")
    return parser


def main() -> None:
    args = build_arg_parser().parse_args()
    full_scan, candidates, rejected, regime = run_group_ab_scan(
        args.group_a_csv,
        args.group_b_csv,
        max_symbols=max(0, args.max_symbols),
        batch_size=max(1, args.batch_size),
        workers=max(1, args.workers),
        explosive_limit=max(0, args.explosive_limit),
        run_explosive=not args.no_explosive,
        use_cache=not args.no_cache,
    )
    sheet_name = write_workbook(args.xlsx_path, full_scan, candidates, rejected, regime)
    print(
        f"\nDone. Sheet '{sheet_name}': {len(full_scan)} liquid stocks, "
        f"{len(candidates)} explosive candidates, {len(rejected)} rejected."
    )


if __name__ == "__main__":
    main()
