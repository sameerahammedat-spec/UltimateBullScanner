"""
Catalyst assessment layer for the afternoon BSE fresh-move scanner.

This module is intentionally dependency-light.  It combines:
1) BSE corporate-announcement headlines (best effort)
2) Google News RSS headlines (best effort)
3) Simple earnings/profit/revenue keyword detection

A missing feed or network failure never disqualifies a technical setup.
The scanner should treat catalyst information as supporting evidence, not proof
that a stock will continue rising.
"""
from __future__ import annotations

from dataclasses import dataclass, asdict
from datetime import datetime
from email.utils import parsedate_to_datetime
from typing import Any, Dict, Iterable, List, Optional, Sequence
from urllib.parse import quote_plus
import re
import xml.etree.ElementTree as ET

import requests

try:
    from afternoon_config import AfternoonConfig, DEFAULT_AFTERNOON_CONFIG
except ImportError:
    AfternoonConfig = Any
    DEFAULT_AFTERNOON_CONFIG = None


@dataclass
class CatalystAssessment:
    found: bool = False
    strength: str = "NONE"
    category: str = "NONE"
    reason: str = ""
    headlines: List[str] = None
    source_count: int = 0
    fresh_today: bool = False
    earnings_signal: bool = False
    score: float = 0.0

    def __post_init__(self) -> None:
        if self.headlines is None:
            self.headlines = []

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


_POSITIVE = {
    "profit", "profits", "profit rises", "profit jumps", "pat rises",
    "revenue rises", "revenue growth", "sales growth", "earnings beat",
    "record profit", "record revenue", "order", "orders", "contract",
    "won order", "large order", "approval", "approved", "clearance",
    "partnership", "joint venture", "acquisition", "merger", "expansion",
    "capacity expansion", "investment", "funding", "dividend", "buyback",
    "guidance raised", "upgrade",
}
_NEGATIVE = {
    "loss", "losses", "profit falls", "profit decline", "revenue falls",
    "revenue decline", "downgrade", "fraud", "default", "penalty",
    "investigation", "resign", "resignation", "fire", "fires", "warning",
    "weak results", "misses estimates", "missed estimates", "debt",
}
_EARNINGS = {
    "quarterly results", "quarter results", "results", "earnings", "profit",
    "pat", "ebitda", "revenue", "sales", "net profit", "operating profit",
}
_CATEGORY_RULES = [
    ("STRONG_EARNINGS", ("profit", "earnings", "quarterly results", "pat", "ebitda", "revenue")),
    ("ORDER_CONTRACT", ("order", "contract", "won order", "purchase order")),
    ("REGULATORY_APPROVAL", ("approval", "approved", "clearance", "regulatory")),
    ("M_AND_A_JV", ("acquisition", "merger", "joint venture", "partnership")),
    ("CAPACITY_EXPANSION", ("expansion", "capacity", "new plant", "investment")),
    ("CORPORATE_ACTION", ("dividend", "buyback", "bonus", "split")),
]


def _safe_text(value: Any) -> str:
    return re.sub(r"\s+", " ", str(value or "")).strip()


def _normalise_title(title: str) -> str:
    return _safe_text(title).lower()


def _parse_rss_items(xml_text: str, max_items: int = 10) -> List[Dict[str, str]]:
    items: List[Dict[str, str]] = []
    try:
        root = ET.fromstring(xml_text)
    except ET.ParseError:
        return items

    for item in root.findall(".//item")[:max_items]:
        title = _safe_text(item.findtext("title"))
        link = _safe_text(item.findtext("link"))
        pub = _safe_text(item.findtext("pubDate"))
        if title:
            items.append({"title": title, "link": link, "published": pub})
    return items


def fetch_google_news(
    symbol: str,
    company_name: str = "",
    max_headlines: int = 6,
    timeout: int = 10,
) -> List[Dict[str, str]]:
    query = " ".join(x for x in [company_name, symbol, "BSE"] if x).strip()
    if not query:
        return []
    url = (
        "https://news.google.com/rss/search?q="
        + quote_plus(query)
        + "&hl=en-IN&gl=IN&ceid=IN:en"
    )
    try:
        response = requests.get(
            url,
            timeout=timeout,
            headers={"User-Agent": "UltimateBullScanner/1.0"},
        )
        response.raise_for_status()
        return _parse_rss_items(response.text, max_items=max_headlines)
    except Exception:
        return []


def fetch_bse_announcements(
    symbol: str,
    company_name: str = "",
    max_pages: int = 1,
    timeout: int = 10,
    **_: Any,
) -> List[Dict[str, str]]:
    """
    Best-effort BSE announcement lookup.

    BSE changes endpoint/response structures periodically.  This function
    deliberately fails closed and returns [] instead of breaking the scanner.
    If your existing repository already has a working BSE announcement helper,
    the scanner can replace this function later without changing the scoring API.
    """
    # Public BSE announcement pages are not guaranteed to expose a stable
    # unauthenticated JSON endpoint. Keep this conservative.
    return []


def _is_today(date_text: str) -> bool:
    if not date_text:
        return False
    try:
        dt = parsedate_to_datetime(date_text)
        return dt.date() == datetime.now().date()
    except Exception:
        return False


def _classify(headlines: Sequence[str]) -> Dict[str, Any]:
    titles = [_normalise_title(x) for x in headlines]
    joined = " | ".join(titles)

    pos = sum(1 for word in _POSITIVE if word in joined)
    neg = sum(1 for word in _NEGATIVE if word in joined)
    earnings = any(word in joined for word in _EARNINGS)

    category = "TECHNICAL_OR_UNKNOWN"
    for name, terms in _CATEGORY_RULES:
        if any(term in joined for term in terms):
            category = name
            break

    if neg > pos and neg >= 2:
        strength = "CAUTION"
        score = -12.0
    elif pos >= 3:
        strength = "STRONG"
        score = 18.0
    elif pos >= 1:
        strength = "MODERATE"
        score = 8.0
    else:
        strength = "NONE"
        score = 0.0

    return {
        "strength": strength,
        "category": category,
        "score": score,
        "earnings_signal": earnings,
    }


def assess_catalyst(
    symbol: str,
    company_name: str = "",
    config: Any = DEFAULT_AFTERNOON_CONFIG,
    **kwargs: Any,
) -> CatalystAssessment:
    max_headlines = int(
        getattr(config, "google_news_max_headlines", 6) if config else 6
    )
    timeout = int(
        getattr(config, "request_timeout_seconds", 12) if config else 12
    )

    news = fetch_google_news(
        symbol=symbol,
        company_name=company_name,
        max_headlines=max_headlines,
        timeout=timeout,
    )
    bse = fetch_bse_announcements(
        symbol=symbol,
        company_name=company_name,
        max_pages=int(getattr(config, "bse_announcement_max_pages", 1) if config else 1),
        timeout=timeout,
    )

    combined_titles = [
        x.get("title", "") for x in (bse + news) if x.get("title")
    ]
    classification = _classify(combined_titles)

    fresh_today = any(
        _is_today(x.get("published", "")) for x in (bse + news)
    )

    # A strong catalyst should be fresh; stale headlines are weaker support.
    score = classification["score"]
    if classification["strength"] == "STRONG" and fresh_today:
        score += 5
    elif classification["strength"] == "MODERATE" and fresh_today:
        score += 3

    headlines = combined_titles[:max_headlines]

    return CatalystAssessment(
        found=bool(headlines),
        strength=classification["strength"],
        category=classification["category"],
        reason=(headlines[0] if headlines else "No material catalyst found; technical move"),
        headlines=headlines,
        source_count=len(headlines),
        fresh_today=fresh_today,
        earnings_signal=classification["earnings_signal"],
        score=float(score),
    )


def catalyst_to_dict(value: CatalystAssessment) -> Dict[str, Any]:
    return value.to_dict()
