"""Canonical Yahoo ticker/data resolver for UltimateBullScanner.

This module separates three concepts:
1) exchange/security identity,
2) Yahoo ticker resolution,
3) interval-specific data availability.

Every caller should use these helpers instead of reconstructing .NS/.BO
suffixes itself.
"""
from __future__ import annotations

import logging
import re
import time
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

import pandas as pd

_LOG = logging.getLogger(__name__)

def _clean_base(value: Any) -> str:
    text = "" if value is None else str(value).strip().upper()
    text = re.sub(r"\.(?:NS|BO)$", "", text, flags=re.I)
    return re.sub(r"\s+", "", text)

def _valid_security_id(value: Any) -> bool:
    """True when a BSE Security Id is plausibly a listed equity symbol.

    BSE Security Id is *not* universally an NSE ticker. In particular, fund
    units and other securities can have short alphanumeric ids (e.g. 08MPR).
    The universe loader filters those instruments; this helper remains
    conservative so obviously non-symbol values never reach Yahoo.
    """
    text = _clean_base(value)
    if not text or text.isdigit():
        return False
    if re.fullmatch(r"IN[A-Z0-9]{10,11}", text):
        return False
    if len(text) > 25 or not re.fullmatch(r"[A-Z][A-Z0-9&.-]*", text):
        return False
    return True

def is_equity_company(stock: Dict[str, Any]) -> bool:
    """Filter the BSE scrip universe to company-equity instruments.

    BSE's ``Instrument=Equity`` also includes fund/ETF/segregated-plan
    securities. Their ISINs commonly start with ``INF`` and their Yahoo
    symbols are not ordinary listed-company symbols. The afternoon scanner is
    explicitly a company-equity scanner, so only ``INE`` equity ISINs are
    admitted when an ISIN is present.
    """
    isin = _clean_base(stock.get("isin"))
    instrument = str(stock.get("instrument") or "").strip().upper()
    if instrument and instrument != "EQUITY":
        return False
    if isin:
        return bool(re.fullmatch(r"INE[A-Z0-9]{9}", isin))
    # Small-cap source rows may omit ISIN; require a plausible symbol and no
    # explicit fund-like naming before allowing them through.
    name = str(stock.get("name") or "").upper()
    fund_markers = ("MUTUAL FUND", " ETF", "FUND", "SEGREGATED", "DEBENTURE", "BOND")
    if any(marker in name for marker in fund_markers):
        return False
    return _valid_security_id(stock.get("security_id") or stock.get("symbol"))

def candidate_tickers(stock: Dict[str, Any]) -> Tuple[str, ...]:
    """Return deterministic Yahoo candidates, identical for diagnostics/scanners.

    Non-company/fund ISINs are a hard stop: never manufacture a Yahoo ticker
    from a BSE security id for those instruments.
    """
    isin = _clean_base(stock.get("isin"))
    if isin and not re.fullmatch(r"INE[A-Z0-9]{9}", isin):
        return tuple()
    candidates: List[str] = []
    primary = str(stock.get("ticker") or "").strip().upper()
    sid = _clean_base(stock.get("security_id"))
    code = str(stock.get("security_code") or "").strip()

    # Respect an explicitly resolved/working ticker first.
    resolved = str(stock.get("resolved_ticker") or "").strip().upper()
    if resolved:
        candidates.append(resolved)

    if primary and (primary.endswith(".NS") or primary.endswith(".BO")):
        candidates.append(primary)

    if _valid_security_id(sid):
        # Security Id is the real trading symbol. Test NSE then BSE.
        candidates.extend((f"{sid}.NS", f"{sid}.BO"))

    base = _clean_base(stock.get("symbol") or "")
    if base and not base.isdigit():
        candidates.extend((f"{base}.NS", f"{base}.BO"))

    if code and re.fullmatch(r"\d{6}", re.sub(r"\D", "", code)):
        numeric = re.sub(r"\D", "", code)
        candidates.append(f"{numeric}.BO")

    for alt in stock.get("alternate_tickers", []) or []:
        if alt:
            candidates.append(str(alt).strip().upper())

    out: List[str] = []
    for t in candidates:
        t = t.strip().upper()
        if t and t not in out:
            out.append(t)
    return tuple(out)

def _normalise_ohlcv(frame: Optional[pd.DataFrame]) -> pd.DataFrame:
    if frame is None or frame.empty:
        return pd.DataFrame()
    frame = frame.copy()
    if isinstance(frame.columns, pd.MultiIndex):
        frame.columns = frame.columns.get_level_values(0)
    # Normalize accidental lower-case column names without changing values.
    rename = {c: str(c).strip().title() for c in frame.columns}
    frame = frame.rename(columns=rename)
    return frame

def has_usable_ohlcv(frame: Optional[pd.DataFrame]) -> bool:
    frame = _normalise_ohlcv(frame)
    if frame.empty:
        return False
    required = ("Open", "High", "Low", "Close")
    if any(c not in frame.columns for c in required):
        return False
    numeric = frame[list(required)].apply(pd.to_numeric, errors="coerce")
    return bool(numeric.notna().all(axis=1).any())

def fetch_history(
    yf_module: Any,
    ticker: str,
    period: str,
    interval: str,
    session: Any = None,
    retries: int = 1,
    retry_delay: float = 0.6,
) -> pd.DataFrame:
    """Fetch and validate actual OHLC data; empty/invalid frames are failures."""
    for attempt in range(retries + 1):
        try:
            obj = yf_module.Ticker(ticker, session=session) if session is not None else yf_module.Ticker(ticker)
            frame = obj.history(period=period, interval=interval, auto_adjust=False)
            frame = _normalise_ohlcv(frame)
            if has_usable_ohlcv(frame):
                return frame
        except Exception as exc:
            if attempt >= retries:
                _LOG.debug("Yahoo history failed for %s: %s", ticker, exc)
        if attempt < retries:
            time.sleep(retry_delay * (attempt + 1))
    return pd.DataFrame()

def resolve_history(
    yf_module: Any,
    stock: Dict[str, Any],
    period: str,
    interval: str,
    session: Any = None,
    retries: int = 1,
) -> Tuple[str, pd.DataFrame, str]:
    """Resolve the first ticker with usable data for this exact interval.

    Returns (ticker, frame, status). Status is one of RESOLVED, NO_DATA.
    """
    candidates = candidate_tickers(stock)
    for ticker in candidates:
        frame = fetch_history(yf_module, ticker, period, interval, session=session, retries=retries)
        if has_usable_ohlcv(frame):
            stock["resolved_ticker"] = ticker
            stock["resolution_status"] = "RESOLVED"
            return ticker, frame, "RESOLVED"
    stock["resolution_status"] = "NO_DATA"
    return "", pd.DataFrame(), "NO_DATA"

def exchange_tickers(stock: Dict[str, Any]) -> Dict[str, str]:
    """Return canonical NSE/BSE candidates for diagnostics."""
    candidates = candidate_tickers(stock)
    return {
        "nse": next((t for t in candidates if t.endswith(".NS")), ""),
        "bse": next((t for t in candidates if t.endswith(".BO")), ""),
    }
