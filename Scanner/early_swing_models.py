"""Typed records used by the Early Swing Rally Scanner."""
from __future__ import annotations

import re
from dataclasses import asdict, dataclass, field
from typing import Dict, List, Optional, Tuple


@dataclass
class UniverseRecord:
    """Canonical security identity used by the scanner.

    ``symbol`` is the stable universe identifier. For BSE master files this is
    normally the six-digit Security Code. ``security_id`` is the exchange's
    human-friendly trading symbol (for example ``ABB`` or ``AEGISLOG``).

    Yahoo Finance is inconsistent for Indian equities: many BSE securities are
    available through ``<Security Id>.BO`` or the dual-listed ``.NS`` ticker,
    while only a minority work through ``<six-digit code>.BO``. The properties
    below centralise that resolution order so every scanner stage uses the same
    logic.
    """

    symbol: str
    ticker: str
    company_name: str
    exchange: str
    bse_group: str = ""
    sector: str = ""
    source_universes: List[str] = field(default_factory=list)
    security_id: str = ""

    @staticmethod
    def _normalise_ticker_base(value: str) -> str:
        text = str(value or "").strip().upper()
        text = re.sub(r"\.(BO|NS)$", "", text, flags=re.IGNORECASE)
        # Preserve & and - because Yahoo uses them for a few Indian symbols.
        return re.sub(r"[^A-Z0-9&-]+", "", text)

    @property
    def normalised_security_id(self) -> str:
        value = self._normalise_ticker_base(self.security_id)
        # An ISIN is not a Yahoo ticker. This guards against malformed CSVs.
        if re.fullmatch(r"INE[A-Z0-9]{9}", value):
            return ""
        if value.isdigit():
            return ""
        return value

    @property
    def stable_key(self) -> str:
        """Key used for deduplication, history maps, and cache files."""
        if self.exchange.upper() == "BSE" or str(self.symbol).isdigit():
            return f"BSE:{str(self.symbol).strip()}"
        base = self._normalise_ticker_base(self.symbol or self.ticker)
        return f"NSE:{base}"

    @property
    def ticker_candidates(self) -> Tuple[str, ...]:
        """Ordered Yahoo ticker strategies, with duplicates removed.

        BSE records:
          1. Security Id + .BO
          2. Security Id + .NS (dual-listed fallback)
          3. Numeric Security Code + .BO

        NSE/non-numeric records:
          1. configured/current ticker
          2. Symbol + .NS
          3. Symbol + .BO
        """
        candidates: List[str] = []
        is_bse = self.exchange.upper() == "BSE" or str(self.symbol).isdigit()
        security_id = self.normalised_security_id

        if is_bse:
            if security_id:
                candidates.extend((f"{security_id}.BO", f"{security_id}.NS"))
            symbol = str(self.symbol or "").strip()
            if symbol:
                candidates.append(f"{symbol}.BO")
        else:
            current = str(self.ticker or "").strip().upper()
            if current:
                if current.endswith((".NS", ".BO")):
                    candidates.append(current)
                else:
                    candidates.append(f"{current}.NS")
            base = self._normalise_ticker_base(self.symbol)
            if base:
                candidates.extend((f"{base}.NS", f"{base}.BO"))

        unique: List[str] = []
        for candidate in candidates:
            candidate = candidate.strip().upper()
            if candidate and candidate not in unique:
                unique.append(candidate)
        return tuple(unique)

    @property
    def preferred_ticker(self) -> str:
        candidates = self.ticker_candidates
        return candidates[0] if candidates else self.ticker

    def refresh_preferred_ticker(self) -> None:
        """Synchronise mutable ``ticker`` after records are merged/enriched."""
        preferred = self.preferred_ticker
        if preferred:
            self.ticker = preferred


@dataclass
class MarketRegime:
    classification: str
    score: float
    above_ema20: bool
    above_sma50: bool
    five_day_return_pct: float
    volatility_pct: float


@dataclass
class EarlySwingCandidate:
    symbol: str
    ticker: str
    company_name: str
    exchange: str
    bse_group: str
    sector: str
    source_universes: List[str]
    setup_date: str
    current_price: Optional[float]
    rally_age: Optional[int]
    setup_type: str
    pivot: Optional[float]
    setup_low: Optional[float]
    entry_low: Optional[float]
    entry_high: Optional[float]
    stop_loss: Optional[float]
    target1: Optional[float]
    target2: Optional[float]
    risk_per_share: Optional[float]
    stop_distance_pct: Optional[float]
    reward_risk: Optional[float]
    position_size: Optional[int]
    nearest_resistance: Optional[float]
    distance_to_resistance_pct: Optional[float]
    one_day_return_pct: Optional[float]
    two_day_return_pct: Optional[float]
    five_day_return_pct: Optional[float]
    ten_day_return_pct: Optional[float]
    breakout_volume_ratio: Optional[float]
    current_volume_ratio: Optional[float]
    rsi: Optional[float]
    adx: Optional[float]
    atr_pct: Optional[float]
    macd_histogram: Optional[float]
    relative_strength_2d: Optional[float]
    relative_strength_5d: Optional[float]
    relative_strength_10d: Optional[float]
    market_regime: str
    extension_risk: float
    false_breakout_risk: float
    data_quality_score: float
    rules_score: float
    confidence: str
    delivery_status: str
    suggested_status: str
    reasons: List[str]
    warning_flags: List[str]
    component_scores: Dict[str, float]
    penalties: List[str]
    rejected: bool = False
    rejection_category: str = ""
    rejection_reason: str = ""
    usable_sessions: int = 0
    latest_data_date: str = ""

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class BacktestTrade:
    symbol: str
    signal_date: str
    entry_date: str
    entry_price: float
    stop_price: float
    target_price: float
    exit_date: str
    exit_price: float
    result: str
    return_pct: float
    holding_sessions: int
    score: float
    setup_type: str
