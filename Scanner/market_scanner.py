"""
Market Scanner (Nifty 500 universe, which already contains all Nifty Smallcap
250 and Nifty Midcap 150 constituents per NSE's index construction).

This is bull_scanner.py with additions:
  - Cap Segment tag (Smallcap vs Large/Midcap) using niftysmallcap250.csv
  - Volume Ratio: today's volume vs avg of the PRIOR 3 days
  - ADX(14) - trend strength (not direction)
  - 20-day linear regression slope (%/day) + R^2 - trend direction & how
    clean/consistent it is (higher R^2 = more reliable trend, not a duration
    forecast)
  - ATR-based Entry / Stop Loss / Target 1 / Target 2

IMPORTANT - what this is and isn't:
  This is standard technical analysis math (ADX, linear regression on price,
  ATR multiples). It is NOT a machine-learning model that predicts how many
  days a trend will last - no honest model can promise that from daily OHLCV
  data alone without serious overfitting. ADX + R^2 tell you how strong/clean
  a trend currently is, which is the best statistically defensible proxy for
  "is this trend likely to persist a bit longer or already exhausted."

  Nothing here is financial advice. Cross-check fundamentals (P/E, ROE,
  Debt/Equity, QoQ growth) on Screener.in before acting - Yahoo's fundamentals
  data is best-effort and sometimes stale/missing.

Setup (once):
    pip install yfinance openpyxl pandas

Usage:
    python market_scanner.py trading_toolkit.xlsx nifty500.csv niftysmallcap250.csv
"""

import sys
import time
import pickle
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date
import pandas as pd
import numpy as np
import requests
import yfinance as yf
from openpyxl import load_workbook
from openpyxl.styles import Font, PatternFill, Alignment, Border, Side
from openpyxl.formatting.rule import CellIsRule
from openpyxl.utils import get_column_letter

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
thin = Side(style="thin", color="BFBFBF")
BORDER = Border(left=thin, right=thin, top=thin, bottom=thin)

MIN_VOLUME = 50000
NIFTY_TICKER = "^NSEI"
TOP_N_DEEPDIVE = 25   # how many top-ranked stocks get the full deep-dive treatment

# Parallel fetch settings. Yahoo Finance is the bottleneck (one network round-trip
# per symbol), not the CSV/merge step - this is what actually speeds up a 250-1000+
# stock scan. Real-world evidence (a 4704-stock run) showed Yahoo blocking the
# client after only ~380 requests at 15 workers - so 15 is NOT a safe default for
# large universes. Lowered to 8. The circuit breaker below is the real safety net;
# this number just controls how fast you *approach* the limit, not whether you
# recover from hitting it.
MAX_WORKERS = 8
FETCH_RETRIES = 1   # one retry on transient failures (timeouts, momentary rate limits)

# A shared session with a real browser User-Agent. Yahoo's anti-bot layer is more
# likely to flag the default python-requests/urllib user agent than a normal
# browser string - this doesn't guarantee avoiding rate limits, but it's a
# legitimate, low-risk reduction in how "botlike" the traffic looks.
_SHARED_SESSION = requests.Session()
_SHARED_SESSION.headers.update({
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/124.0 Safari/537.36",
})


class _RateLimitGuard:
    """Shared circuit breaker across all worker threads.

    Yahoo Finance gives no clean 'you are being rate-limited' signal through
    yfinance - once it starts throttling you, perfectly valid, liquid symbols
    (Shriram Finance, PCBL, etc.) just silently come back empty, identical to
    a real delisted stock. The tell isn't any single failure - it's a WALL of
    consecutive failures right after a stretch of real successes, which is
    exactly what a burst of parallel workers can trigger partway through a
    large scan (this is what the 4704-stock run showed: clean through ~375,
    then 100% failures from ~400 onward).

    This tracks consecutive failures across all threads. Once that streak
    crosses a threshold, every worker pauses (checked before their next
    request) for a cooldown period, then resumes automatically. If it keeps
    happening, the cooldown escalates (doubles, capped) rather than retrying
    at the same pace that got it blocked in the first place.
    """

    def __init__(self, failure_threshold=12, cooldown_seconds=90, max_cooldown=900):
        self._lock = threading.Lock()
        self._consecutive_failures = 0
        self._paused_until = 0.0
        self._failure_threshold = failure_threshold
        self._base_cooldown = cooldown_seconds
        self._current_cooldown = cooldown_seconds
        self._max_cooldown = max_cooldown

    def wait_if_paused(self):
        while True:
            with self._lock:
                remaining = self._paused_until - time.time()
            if remaining <= 0:
                return
            time.sleep(min(remaining, 5))

    def record_success(self):
        with self._lock:
            self._consecutive_failures = 0
            self._current_cooldown = self._base_cooldown  # things are working again - reset the backoff

    def record_failure(self):
        with self._lock:
            self._consecutive_failures += 1
            if self._consecutive_failures >= self._failure_threshold and time.time() >= self._paused_until:
                cooldown = self._current_cooldown
                print(f"\n[rate-limit guard] {self._consecutive_failures} consecutive fetch failures - "
                      f"this almost always means Yahoo Finance is throttling this connection, not that "
                      f"this many stocks in a row are genuinely delisted. Pausing ALL workers for "
                      f"{cooldown:.0f}s to cool down, then resuming automatically...\n")
                self._paused_until = time.time() + cooldown
                self._consecutive_failures = 0
                self._current_cooldown = min(cooldown * 2, self._max_cooldown)  # escalate if it recurs


_rate_guard = _RateLimitGuard()

HEADERS = ["Symbol", "Name", "Sector", "Cap Segment", "Close", "% Chg", "Volume",
           "Vol Ratio (vs 3d avg)", "Mkt Cap (Cr)",
           "Liquidity OK?", "Above 50DMA?", "Above 200DMA?", "RSI(14)", "RSI Healthy?",
           "ATR(14)", "Vol Spike?", "% Down from 52wH", "Near 52wH?",
           "RS vs Nifty 1M %", "RS vs Nifty 3M %", "RS Positive?",
           "P/E", "ROE %", "Debt/Equity", "QoQ Profit Gr %",
           "Score (0-9)", "Verdict"]

DEEPDIVE_HEADERS = ["Symbol", "Name", "Cap Segment", "Close", "Score", "Verdict",
                     "Vol Ratio (vs 3d avg)", "ADX(14)", "Trend Strength",
                     "Trend Direction", "20d Slope %/day", "Trend R^2",
                     "Trend Quality (ADX x R^2)",
                     "Entry", "Stop Loss", "Target 1", "Target 2", "Risk:Reward T1/T2"]


def rsi(series, period=14):
    delta = series.diff()
    gain = delta.clip(lower=0)
    loss = -delta.clip(upper=0)
    avg_gain = gain.rolling(period).mean()
    avg_loss = loss.rolling(period).mean()
    rs = avg_gain / avg_loss
    return 100 - (100 / (1 + rs))


def atr(df, period=14):
    high, low, close = df["High"], df["Low"], df["Close"]
    prev_close = close.shift(1)
    tr = pd.concat([(high - low), (high - prev_close).abs(), (low - prev_close).abs()], axis=1).max(axis=1)
    return tr.rolling(period).mean()


def adx(df, period=14):
    """Standard Wilder ADX. Measures trend STRENGTH (0-100), not direction."""
    high, low, close = df["High"], df["Low"], df["Close"]
    up_move = high.diff()
    down_move = -low.diff()
    plus_dm = np.where((up_move > down_move) & (up_move > 0), up_move, 0.0)
    minus_dm = np.where((down_move > up_move) & (down_move > 0), down_move, 0.0)
    tr = pd.concat([(high - low), (high - close.shift(1)).abs(), (low - close.shift(1)).abs()], axis=1).max(axis=1)
    atr_s = tr.rolling(period).mean()
    plus_di = 100 * pd.Series(plus_dm, index=df.index).rolling(period).mean() / atr_s
    minus_di = 100 * pd.Series(minus_dm, index=df.index).rolling(period).mean() / atr_s
    dx = 100 * (plus_di - minus_di).abs() / (plus_di + minus_di)
    return dx.rolling(period).mean()


def trend_regression(close, window=20):
    """Linear regression of log(close) over the last `window` days.
    Returns (slope_pct_per_day, r_squared). R^2 close to 1 = clean, consistent
    trend. R^2 close to 0 = choppy/no reliable trend, regardless of direction."""
    y = np.log(close.iloc[-window:].values)
    if len(y) < window or np.any(np.isnan(y)):
        return None, None
    x = np.arange(window)
    slope, intercept = np.polyfit(x, y, 1)
    y_hat = slope * x + intercept
    ss_res = np.sum((y - y_hat) ** 2)
    ss_tot = np.sum((y - np.mean(y)) ** 2)
    r2 = 1 - ss_res / ss_tot if ss_tot > 0 else 0.0
    slope_pct_per_day = (np.exp(slope) - 1) * 100
    return slope_pct_per_day, r2


def trend_strength_label(adx_val):
    if adx_val is None or np.isnan(adx_val):
        return "N/A"
    if adx_val < 20:
        return "Weak / Range-bound"
    if adx_val < 40:
        return "Moderate"
    return "Strong"


def to_scalar(x):
    if isinstance(x, pd.Series):
        x = x.iloc[-1] if len(x) else None
    if x is None or pd.isna(x):
        return None
    return float(x)


def fetch_history(nse_symbol, period="1y"):
    """Tries the NSE listing (.NS) first since it's usually more liquid/reliable
    on Yahoo Finance; falls back to the BSE listing (.BO) if the stock isn't on
    NSE at all (common for smaller BSE-only smallcaps). Retries once per
    exchange on transient failures. Checks/reports into the shared rate-limit
    guard so a burst of throttling gets detected and cooled down instead of
    silently eating thousands of otherwise-valid stocks.
    Returns (df, ticker_str) or (None, None)."""
    for suffix in (".NS", ".BO"):
        ticker_str = nse_symbol.strip() + suffix
        for attempt in range(FETCH_RETRIES + 1):
            _rate_guard.wait_if_paused()
            try:
                tk = yf.Ticker(ticker_str, session=_SHARED_SESSION)
                df = tk.history(period=period, interval="1d", auto_adjust=True)
                if not df.empty and len(df) >= 60:
                    _rate_guard.record_success()
                    return df, ticker_str, tk
                _rate_guard.record_failure()
                break  # empty result isn't transient - move to next suffix, don't retry
            except Exception:
                _rate_guard.record_failure()
                if attempt < FETCH_RETRIES:
                    time.sleep(0.5 * (attempt + 1))
                    continue
                break
    return None, None, None


def get_nifty_returns():
    tk = yf.Ticker(NIFTY_TICKER)
    df = tk.history(period="1y", interval="1d", auto_adjust=True)
    close = df["Close"]
    ret_1m = (close.iloc[-1] / close.iloc[-22] - 1) * 100 if len(close) > 22 else None
    ret_3m = (close.iloc[-1] / close.iloc[-63] - 1) * 100 if len(close) > 63 else None
    return ret_1m, ret_3m


def analyze_symbol(nse_symbol, nifty_1m, nifty_3m, return_series=False):
    df, ticker_str, tk = fetch_history(nse_symbol)
    if df is None:
        return None

    if isinstance(df.columns, pd.MultiIndex):
        df.columns = df.columns.get_level_values(0)

    close, volume = df["Close"], df["Volume"]
    last_close = to_scalar(close.iloc[-1])
    prev_close = to_scalar(close.iloc[-2]) if len(close) > 1 else None
    pct_change = ((last_close - prev_close) / prev_close * 100) if (last_close and prev_close) else None

    dma50 = to_scalar(close.rolling(50).mean().iloc[-1])
    dma200 = to_scalar(close.rolling(200).mean().iloc[-1]) if len(close) >= 200 else None
    rsi14 = to_scalar(rsi(close, 14).iloc[-1])
    atr14 = to_scalar(atr(df, 14).iloc[-1])
    adx14 = to_scalar(adx(df, 14).iloc[-1])
    slope_pct, r2 = trend_regression(close, 20)
    # Trend Quality Score: strength (ADX, 0-100) weighted by how clean/consistent
    # the trend is (R^2, 0-1). A stock can have high ADX but low R^2 (a couple of
    # violent single-day moves, not a real trend) or vice versa; this single
    # number rewards only the combination of BOTH being high.
    trend_quality = (adx14 * r2) if (adx14 is not None and r2 is not None) else None

    avg_vol_20 = to_scalar(volume.rolling(20).mean().iloc[-1])
    last_vol = to_scalar(volume.iloc[-1])
    vol_spike = (last_vol is not None and avg_vol_20 and last_vol > 1.5 * avg_vol_20)
    vol_ratio_20d = (last_vol / avg_vol_20) if (last_vol is not None and avg_vol_20) else None

    # Volume ratio: today vs avg of the PRIOR 3 days (excludes today itself)
    vol_ratio_3d = None
    if len(volume) > 4:
        prior_3d_avg = to_scalar(volume.iloc[-4:-1].mean())
        if prior_3d_avg and prior_3d_avg > 0 and last_vol is not None:
            vol_ratio_3d = last_vol / prior_3d_avg

    high_52w = to_scalar(close.rolling(252).max().iloc[-1]) if len(close) >= 60 else to_scalar(close.max())
    down_from_high = ((high_52w - last_close) / high_52w * 100) if (high_52w and last_close) else None

    stock_1m = (last_close / to_scalar(close.iloc[-22]) - 1) * 100 if len(close) > 22 else None
    stock_3m = (last_close / to_scalar(close.iloc[-63]) - 1) * 100 if len(close) > 63 else None
    rs_1m = (stock_1m - nifty_1m) if (stock_1m is not None and nifty_1m is not None) else None
    rs_3m = (stock_3m - nifty_3m) if (stock_3m is not None and nifty_3m is not None) else None

    roe = pe = debt_equity = qoq_growth = mkt_cap_cr = None
    try:
        info = tk.info
        if info.get("returnOnEquity") is not None:
            roe = info["returnOnEquity"] * 100
        pe = info.get("trailingPE")
        debt_equity = info.get("debtToEquity")
        if info.get("marketCap") is not None:
            mkt_cap_cr = info["marketCap"] / 1e7
    except Exception:
        pass
    try:
        qf = tk.quarterly_financials
        if qf is not None and "Net Income" in qf.index and qf.shape[1] >= 2:
            latest_q, prev_q = qf.loc["Net Income"].iloc[0], qf.loc["Net Income"].iloc[1]
            if prev_q and prev_q != 0:
                qoq_growth = (latest_q - prev_q) / abs(prev_q) * 100
    except Exception:
        pass

    # --- Entry / Stop Loss / Target levels (ATR-based, direction-aware) ---
    entry = stop_loss = target1 = target2 = risk_reward = None
    trend_direction = "N/A"
    if slope_pct is not None and atr14 is not None and last_close is not None:
        trend_direction = "Uptrend" if slope_pct > 0 else "Downtrend"
        entry = last_close
        if trend_direction == "Uptrend":
            stop_loss = entry - 1.5 * atr14
            target1 = entry + 1.5 * atr14
            target2 = entry + 3.0 * atr14
        else:
            stop_loss = entry + 1.5 * atr14
            target1 = entry - 1.5 * atr14
            target2 = entry - 3.0 * atr14
        risk_reward = "1:1 / 1:2"  # 1.5 ATR risk vs 1.5/3.0 ATR reward

    result = dict(
        close=last_close, pct_change=pct_change, volume=last_vol, mkt_cap_cr=mkt_cap_cr,
        vol_ratio_3d=vol_ratio_3d, vol_ratio_20d=vol_ratio_20d,
        liquidity_ok=(last_vol is not None and last_vol >= MIN_VOLUME),
        above_50dma=(dma50 is not None and last_close > dma50),
        above_200dma=(dma200 is not None and last_close > dma200) if dma200 is not None else None,
        rsi=rsi14, rsi_healthy=(rsi14 is not None and 50 <= rsi14 <= 70), atr=atr14,
        adx=adx14, trend_strength=trend_strength_label(adx14),
        trend_direction=trend_direction, trend_slope_pct=slope_pct, trend_r2=r2,
        trend_quality=trend_quality,
        entry=entry, stop_loss=stop_loss, target1=target1, target2=target2, risk_reward=risk_reward,
        vol_spike=vol_spike, down_from_high=down_from_high,
        near_high=(down_from_high is not None and down_from_high <= 10),
        rs_1m=rs_1m, rs_3m=rs_3m, rs_positive=(rs_3m is not None and rs_3m > 0),
        pe=pe, roe=roe, roe_ok=(roe is not None and roe > 15), debt_equity=debt_equity,
        qoq_growth=qoq_growth, qoq_ok=(qoq_growth is not None and qoq_growth > 0),
    )
    if return_series:
        result["close_series"] = close.values
        result["volume_series"] = volume.values
    return result


def analyze_universe_parallel(symbols, nifty_1m, nifty_3m, return_series=False,
                               max_workers=MAX_WORKERS, verbose=True):
    """Runs analyze_symbol() across all symbols concurrently using a thread
    pool. This is an I/O-bound workload (waiting on Yahoo Finance's servers,
    not CPU), which is exactly the case where Python threads help despite the
    GIL - each thread releases it while blocked on network I/O. Returns a
    dict {symbol: result_dict_or_None}, in completion order (not input order -
    callers re-sort by score/rank anyway, so this doesn't matter downstream).
    """
    results = {}
    total = len(symbols)
    completed = 0
    ok_count = 0

    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        future_to_symbol = {
            executor.submit(analyze_symbol, sym, nifty_1m, nifty_3m, return_series): sym
            for sym in symbols
        }
        for future in as_completed(future_to_symbol):
            sym = future_to_symbol[future]
            completed += 1
            try:
                d = future.result()
            except Exception as e:
                d = None
                if verbose:
                    print(f"[{completed}/{total}] {sym}: ERROR ({e})")
            else:
                if d is not None:
                    ok_count += 1
                if verbose and (completed % 25 == 0 or completed == total):
                    print(f"[{completed}/{total}] fetched, {ok_count} usable so far "
                          f"(last: {sym} {'OK' if d else 'no data'})")
            results[sym] = d

    return results


def score_row(d):
    checks = [d["liquidity_ok"], d["above_50dma"], bool(d["above_200dma"]),
              d["rsi_healthy"], d["vol_spike"], d["near_high"],
              d["rs_positive"], d["roe_ok"], d["qoq_ok"]]
    return sum(1 for c in checks if c)


def verdict(score):
    if score >= 7:
        return "STRONG SETUP"
    elif score >= 5:
        return "WATCHLIST"
    return "AVOID"


def find_col(df, names):
    for c in df.columns:
        cl = c.strip().lower()
        for n in names:
            if n == cl or n in cl:
                return c
    return None


def load_universe(paths):
    """Loads and merges universe CSVs. Tags each symbol's Cap Segment based on
    which source file(s) it came from:
      - filename contains 'bse' AND 'smallcap'  -> "BSE Smallcap"
      - filename contains 'smallcap' (not bse)  -> "NSE Smallcap"
      - otherwise                                -> "Large/Midcap"
    If a symbol appears in more than one smallcap file, BSE Smallcap wins the
    label (since that's usually the more specific/relevant tag when a stock is
    also NSE-listed)."""
    frames = []
    nse_smallcap_symbols = set()
    bse_smallcap_symbols = set()
    for p in paths:
        df = pd.read_csv(p)
        sym_col = find_col(df, ("symbol", "nse code", "nsecode", "ticker", "security id", "scrip code"))
        if not sym_col:
            print(f"  [!] Skipping {p}: no Symbol column found ({list(df.columns)})")
            continue
        name_col = find_col(df, ("company name", "name", "stock name", "security name", "constituents"))
        sector_col = find_col(df, ("industry", "sector"))
        syms = df[sym_col].astype(str).str.strip()
        out = pd.DataFrame({
            "symbol": syms,
            "name": df[name_col].astype(str).str.strip() if name_col else df[sym_col],
            "sector": df[sector_col].astype(str).str.strip() if sector_col else "",
        })
        frames.append(out)
        fname = p.lower()
        if "smallcap" in fname and "bse" in fname:
            bse_smallcap_symbols.update(syms.tolist())
        elif "smallcap" in fname:
            nse_smallcap_symbols.update(syms.tolist())

    merged = pd.concat(frames, ignore_index=True).drop_duplicates(subset="symbol")

    def tag(s):
        if s in bse_smallcap_symbols:
            return "BSE Smallcap"
        if s in nse_smallcap_symbols:
            return "NSE Smallcap"
        return "Large/Midcap"

    merged["cap_segment"] = merged["symbol"].apply(tag)
    return merged


def write_table(ws, start_row, rows, title=None):
    r = start_row
    if title:
        ws.cell(row=r, column=1, value=title).font = SEC_FONT
        for c in range(1, len(HEADERS) + 1):
            ws.cell(row=r, column=c).fill = SEC_FILL
        ws.merge_cells(start_row=r, start_column=1, end_row=r, end_column=len(HEADERS))
        r += 1

    for i, h in enumerate(HEADERS, start=1):
        c = ws.cell(row=r, column=i, value=h)
        c.font = HDR_FONT
        c.fill = HDR_FILL
        c.alignment = Alignment(horizontal="center", vertical="center", wrap_text=True)
        c.border = BORDER
    ws.row_dimensions[r].height = 32
    header_row = r
    r += 1

    def yn(v):
        return "N/A" if v is None else ("Y" if v else "N")

    def num(v, nd=1):
        return round(v, nd) if v is not None else "N/A"

    for idx, d in enumerate(rows):
        vals = [d["symbol"], d["name"], d.get("sector", ""), d.get("cap_segment", ""),
                num(d["close"], 2), num(d["pct_change"], 2),
                d["volume"], num(d.get("vol_ratio_3d"), 2), num(d["mkt_cap_cr"], 0),
                yn(d["liquidity_ok"]), yn(d["above_50dma"]),
                yn(d["above_200dma"]), num(d["rsi"], 1), yn(d["rsi_healthy"]), num(d["atr"], 2),
                yn(d["vol_spike"]), num(d["down_from_high"], 1), yn(d["near_high"]),
                num(d["rs_1m"], 1), num(d["rs_3m"], 1), yn(d["rs_positive"]),
                num(d["pe"], 1), num(d["roe"], 1), num(d["debt_equity"], 2), num(d["qoq_growth"], 1),
                d["score"], d["verdict"]]
        for c, v in enumerate(vals, start=1):
            cell = ws.cell(row=r, column=c, value=v)
            cell.font = BLACK_B if c == len(HEADERS) else BLACK
            cell.border = BORDER
            cell.alignment = Alignment(horizontal="center")
            if idx % 2 == 1:
                cell.fill = ALT_FILL
        r += 1

    last_row = r - 1
    verdict_col = get_column_letter(len(HEADERS))
    if last_row >= header_row + 1:
        ws.conditional_formatting.add(f"{verdict_col}{header_row+1}:{verdict_col}{last_row}",
            CellIsRule(operator="equal", formula=['"STRONG SETUP"'], fill=GOOD_FILL))
        ws.conditional_formatting.add(f"{verdict_col}{header_row+1}:{verdict_col}{last_row}",
            CellIsRule(operator="equal", formula=['"WATCHLIST"'], fill=WATCH_FILL))
        ws.conditional_formatting.add(f"{verdict_col}{header_row+1}:{verdict_col}{last_row}",
            CellIsRule(operator="equal", formula=['"AVOID"'], fill=AVOID_FILL))

    return r + 1


def write_deepdive_table(ws, start_row, rows, title=None):
    r = start_row
    if title:
        ws.cell(row=r, column=1, value=title).font = SEC_FONT
        for c in range(1, len(DEEPDIVE_HEADERS) + 1):
            ws.cell(row=r, column=c).fill = SEC_FILL
        ws.merge_cells(start_row=r, start_column=1, end_row=r, end_column=len(DEEPDIVE_HEADERS))
        r += 1

    for i, h in enumerate(DEEPDIVE_HEADERS, start=1):
        c = ws.cell(row=r, column=i, value=h)
        c.font = HDR_FONT
        c.fill = HDR_FILL
        c.alignment = Alignment(horizontal="center", vertical="center", wrap_text=True)
        c.border = BORDER
    ws.row_dimensions[r].height = 32
    r += 1

    def num(v, nd=2):
        return round(v, nd) if v is not None else "N/A"

    for idx, d in enumerate(rows):
        vals = [d["symbol"], d["name"], d.get("cap_segment", ""), num(d["close"], 2),
                d["score"], d["verdict"], num(d.get("vol_ratio_3d"), 2), num(d.get("adx"), 1),
                d.get("trend_strength", "N/A"), d.get("trend_direction", "N/A"),
                num(d.get("trend_slope_pct"), 2), num(d.get("trend_r2"), 2),
                num(d.get("trend_quality"), 1),
                num(d.get("entry"), 2), num(d.get("stop_loss"), 2),
                num(d.get("target1"), 2), num(d.get("target2"), 2), d.get("risk_reward", "N/A")]
        for c, v in enumerate(vals, start=1):
            cell = ws.cell(row=r, column=c, value=v)
            cell.font = BLACK
            cell.border = BORDER
            cell.alignment = Alignment(horizontal="center")
            if idx % 2 == 1:
                cell.fill = ALT_FILL
        r += 1
    return r + 1


def run_scan(xlsx_path, universe_paths, verbose=True):
    """Runs the full scan and returns (full_scan, early_momentum, deep_dive) lists.
    Does NOT write to Excel - call write_to_workbook separately (or use main())."""
    universe = load_universe(universe_paths)
    symbols = universe["symbol"].tolist()
    meta = universe.set_index("symbol").to_dict(orient="index")

    if verbose:
        print(f"Universe loaded: {len(symbols)} unique symbols across {len(universe_paths)} list(s)")
        print("Fetching Nifty 50 benchmark returns...")
    nifty_1m, nifty_3m = get_nifty_returns()
    if verbose:
        print(f"Nifty 1M: {nifty_1m:.2f}%  |  Nifty 3M: {nifty_3m:.2f}%\n")

    results = []
    fetched = analyze_universe_parallel(symbols, nifty_1m, nifty_3m, verbose=verbose)
    for sym, d in fetched.items():
        if d is None:
            continue
        if not d["liquidity_ok"]:
            continue
        d["symbol"] = sym
        d["name"] = meta.get(sym, {}).get("name", sym)
        d["sector"] = meta.get(sym, {}).get("sector", "")
        d["cap_segment"] = meta.get(sym, {}).get("cap_segment", "")
        d["score"] = score_row(d)
        d["verdict"] = verdict(d["score"])
        results.append(d)

    full_scan = sorted(results, key=lambda x: x["score"], reverse=True)
    early_momentum = sorted(
        [d for d in results if d["rsi"] is not None and 40 <= d["rsi"] <= 50 and d["liquidity_ok"]],
        key=lambda x: (x["rs_3m"] if x["rs_3m"] is not None else -999), reverse=True
    )
    deep_dive = [d for d in full_scan if d["verdict"] in ("STRONG SETUP", "WATCHLIST")][:TOP_N_DEEPDIVE]

    return full_scan, early_momentum, deep_dive


def main(xlsx_path, universe_paths):
    full_scan, early_momentum, deep_dive = run_scan(xlsx_path, universe_paths)

    today = date.today()
    sheet_name = f"Scan_{today.strftime('%d%b%y')}"

    backup_path = "bull_scan_backup.pkl"
    with open(backup_path, "wb") as f:
        pickle.dump({"date": today, "sheet_name": sheet_name,
                     "full_scan": full_scan, "early_momentum": early_momentum,
                     "deep_dive": deep_dive}, f)
    print(f"Backup saved to {backup_path} (safe even if the Excel write below fails)\n")

    try:
        write_to_workbook(xlsx_path, sheet_name, today, full_scan, early_momentum, deep_dive)
    except PermissionError:
        print(f"\n[!] Could not save {xlsx_path} — it's likely still open in Excel/LibreOffice.")
        print("    1. Close the Excel file completely.")
        print("    2. Run:  python write_backup_to_excel.py " + xlsx_path)
        sys.exit(1)

    print(f"\nDone. Sheet '{sheet_name}' created with {len(full_scan)} full-scan stocks, "
          f"{len(early_momentum)} early-momentum candidates, {len(deep_dive)} deep-dive picks.")
    print("P/E, ROE, Debt/Equity, QoQ Growth are best-effort from Yahoo Finance — cross-check on Screener.in.")


def write_to_workbook(xlsx_path, sheet_name, today, full_scan, early_momentum, deep_dive):
    wb = load_workbook(xlsx_path)
    if sheet_name in wb.sheetnames:
        del wb[sheet_name]
    ws = wb.create_sheet(sheet_name, 1)
    ws.sheet_view.showGridLines = False

    ws.cell(row=1, column=1, value=f"Market Scan — {today.strftime('%d %b %Y')}").font = Font(name=FONT, size=14, bold=True, color="1F3864")
    ws.merge_cells(start_row=1, start_column=1, end_row=1, end_column=len(HEADERS))

    next_row = write_table(ws, 3, full_scan, title=f"FULL SCAN — {len(full_scan)} stocks passed liquidity filter, ranked by score")
    next_row = write_table(ws, next_row, early_momentum,
                title=f"EARLY MOMENTUM WATCHLIST (RSI 40-50) — {len(early_momentum)} stocks turning up, not yet extended")
    write_deepdive_table(ws, next_row, deep_dive,
                title=f"TOP CANDIDATES — DEEP DIVE (trend, entry/stop/targets) — top {len(deep_dive)}")

    for i in range(1, len(HEADERS) + 1):
        ws.column_dimensions[get_column_letter(i)].width = 13
    ws.column_dimensions["B"].width = 28
    ws.column_dimensions["C"].width = 18
    ws.freeze_panes = "D4"

    wb.save(xlsx_path)


if __name__ == "__main__":
    if len(sys.argv) < 3:
        print("Usage: python market_scanner.py trading_toolkit.xlsx nifty500.csv [niftysmallcap250.csv ...]")
        sys.exit(1)
    main(sys.argv[1], sys.argv[2:])