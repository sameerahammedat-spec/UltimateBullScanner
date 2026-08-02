from __future__ import annotations

from datetime import date
from pathlib import Path

import numpy as np
import pandas as pd
import pytest
from openpyxl import Workbook, load_workbook

from early_swing_backtest import summarize_trades
from early_swing_config import DEFAULT_EARLY_SWING_CONFIG
from early_swing_engine import analyze_early_swing
from early_swing_features import classify_market_regime
from early_swing_models import UniverseRecord
from early_swing_report import write_early_swing_report
from early_swing_universe import load_and_deduplicate_universes, load_universe_file


def make_breakout(age=1, extended=False, seed=7):
    rng = np.random.default_rng(seed)
    n = 180
    close = np.linspace(80, 100, n) + rng.normal(0, 0.35, n)
    # Build a 25-session tight base below 105.
    base_start = n - 27
    close[base_start:n-2] = 101.5 + np.sin(np.linspace(0, 5, n-2-base_start)) * 1.2 + rng.normal(0, 0.15, n-2-base_start)
    open_ = close + rng.normal(0, 0.15, n)
    high = np.maximum(open_, close) + 0.7
    low = np.minimum(open_, close) - 0.7
    volume = np.full(n, 200_000.0) + rng.normal(0, 15_000, n)
    signal_idx = n - age
    # Prior pivot near 103.4.
    high[base_start:signal_idx] = np.minimum(high[base_start:signal_idx], 103.4)
    close[signal_idx] = 105.8
    open_[signal_idx] = 102.8
    high[signal_idx] = 106.2
    low[signal_idx] = 102.5
    volume[signal_idx] = 480_000
    if age == 2:
        close[-1] = 106.4
        open_[-1] = 105.6
        high[-1] = 107.0
        low[-1] = 105.2
        volume[-1] = 250_000
    if extended:
        close[-5:] = [104, 110, 117, 125, 134]
        open_[-5:] = [102, 106, 112, 119, 128]
        high[-5:] = close[-5:] + 1.5
        low[-5:] = open_[-5:] - 1.0
        volume[-5:] = 600_000
    idx = pd.bdate_range("2025-01-01", periods=n)
    return pd.DataFrame({"Open": open_, "High": high, "Low": low, "Close": close, "Volume": volume}, index=idx)


def make_benchmark(n=180):
    idx = pd.bdate_range("2025-01-01", periods=n)
    close = np.linspace(100, 112, n)
    return pd.DataFrame({"Open": close-0.2, "High": close+0.5, "Low": close-0.5,
                         "Close": close, "Volume": 1_000_000}, index=idx)


@pytest.fixture
def record():
    return UniverseRecord("500001", "500001.BO", "Test Ltd", "BSE", "A", "Industrials", ["BSE Group A"])


def test_group_a_csv_with_trailing_commas(tmp_path):
    p = tmp_path / "Group_A.csv"
    p.write_text("Security Code,Issuer Name,Security Id,Security Name,Status,Group,Face Value,ISIN No,Instrument\n"
                 "500002,ABB India Limited,ABB,ABB India Ltd,Active,A ,2.00,INE117A01022,Equity,,,,,\n", encoding="utf-8")
    rows = load_universe_file(str(p))
    assert rows[0].symbol == "500002"
    assert rows[0].company_name == "ABB India Ltd"
    assert rows[0].bse_group.strip() == "A"
    assert rows[0].security_id == "ABB"
    assert rows[0].ticker == "ABB.BO"
    assert rows[0].ticker_candidates == ("ABB.BO", "ABB.NS", "500002.BO")


def test_bse1000_csv_headers(tmp_path):
    p = tmp_path / "bse1000_clean.csv"
    p.write_text("Constituents,Symbol,Macro-Economic Sector\n360 ONE WAM LIMITED,542772,Financial Services\n", encoding="utf-8")
    rows = load_universe_file(str(p))
    assert rows[0].company_name == "360 ONE WAM LIMITED"
    assert rows[0].sector == "Financial Services"


def test_dedup_merges_sources(tmp_path):
    a = tmp_path / "Group_A.csv"
    b = tmp_path / "bse1000_clean.csv"
    a.write_text("Security Code,Issuer Name,Security Id,Security Name,Status,Group,Face Value,ISIN No,Instrument\n"
                 "500002,ABB India Limited,ABB,ABB India Ltd,Active,A,2,ISIN,Equity\n", encoding="utf-8")
    b.write_text("Constituents,Symbol,Macro-Economic Sector\nABB INDIA LIMITED,500002,Industrials\n", encoding="utf-8")
    rows = load_and_deduplicate_universes([str(a), str(b)])
    assert len(rows) == 1
    assert len(rows[0].source_universes) == 2
    assert rows[0].security_id == "ABB"
    assert rows[0].ticker == "ABB.BO"


def test_rally_age_one_detected(record):
    df = make_breakout(age=1)
    c = analyze_early_swing(record, df, make_benchmark(), analysis_date=df.index[-1].date())
    assert not c.rejected
    assert c.rally_age == 1
    assert c.setup_type in {"Consolidation Breakout", "Inside-Bar Breakout", "NR7 Breakout"}
    assert c.rules_score >= 65


def test_rally_age_two_followthrough_detected(record):
    df = make_breakout(age=2)
    c = analyze_early_swing(record, df, make_benchmark(), analysis_date=df.index[-1].date())
    assert not c.rejected
    assert c.rally_age == 2


def test_random_green_candle_without_base_rejected(record):
    rng = np.random.default_rng(99)
    n = 180
    close = 100 + np.cumsum(rng.normal(0, 2.5, n))
    close[-1] = close[-2] * 1.05
    idx = pd.bdate_range("2025-01-01", periods=n)
    df = pd.DataFrame({"Open": close-rng.normal(0,1,n), "High": close+2, "Low": close-2,
                       "Close": close, "Volume": np.r_[np.full(n-1,200000),500000]}, index=idx)
    c = analyze_early_swing(record, df, make_benchmark(), analysis_date=df.index[-1].date())
    assert c.rejected


def test_extended_rally_rejected(record):
    df = make_breakout(age=1, extended=True)
    c = analyze_early_swing(record, df, make_benchmark(), analysis_date=df.index[-1].date())
    assert c.rejected
    assert c.rejection_category in {"EXTENDED", "NO_SETUP", "POOR_RISK_REWARD"}


def test_no_lookahead(record):
    full = make_breakout(age=1)
    cutoff = full.index[-10]
    history = full.loc[:cutoff].copy()
    # Make the cutoff itself a breakout by using a dedicated generated frame ending there.
    history = make_breakout(age=1).iloc[:-9]
    benchmark = make_benchmark().iloc[:len(history)]
    first = analyze_early_swing(record, history, benchmark, analysis_date=history.index[-1].date()).to_dict()
    mutated = full.copy()
    mutated.iloc[-8:, mutated.columns.get_loc("Close")] *= 4
    second = analyze_early_swing(record, history, benchmark, analysis_date=history.index[-1].date()).to_dict()
    assert first == second


def test_report_writes_sheet(tmp_path, record):
    df = make_breakout(age=1)
    benchmark = make_benchmark()
    c = analyze_early_swing(record, df, benchmark, analysis_date=df.index[-1].date())
    xlsx = tmp_path / "toolkit.xlsx"
    wb = Workbook(); wb.save(xlsx)
    regime = classify_market_regime(benchmark)
    sheet = write_early_swing_report(str(xlsx), [c], regime, date(2026, 7, 31))
    loaded = load_workbook(xlsx, read_only=True)
    assert sheet in loaded.sheetnames
    ws = loaded[sheet]
    assert ws["A1"].value.startswith("Early Swing Rally Scanner")


def test_summary_empty_trades():
    summary = summarize_trades([])
    assert summary["trades"] == 0
    assert summary["win_rate_pct"] == 0


def test_latest_bse_gainer_sheet_loader(tmp_path):
    from early_swing_universe import load_latest_bse_gainers_from_workbook
    xlsx = tmp_path / "toolkit.xlsx"
    wb = Workbook()
    ws = wb.active
    ws.title = "BSEGain_31Jul26"
    ws.append(["BSE Top Gainers"])
    ws.append([])
    ws.append(["ALL BSE GAINERS"])
    ws.append(["Symbol", "Name", "Cap Segment", "Close"])
    ws.append(["506590", "PCBL Chemical Ltd", "BSE Smallcap", 365.85])
    ws.append([])
    ws.append(["BSE SMALLCAP GAINERS — DEEP DIVE"])
    wb.save(xlsx)
    rows = load_latest_bse_gainers_from_workbook(str(xlsx))
    assert len(rows) == 1
    assert rows[0].symbol == "506590"
    assert rows[0].company_name == "PCBL Chemical Ltd"
    assert rows[0].source_universes == ["BSE Gainer"]



def test_merge_enriches_numeric_bse1000_ticker_with_group_security_id(tmp_path):
    bse1000 = tmp_path / "bse1000_clean.csv"
    group_a = tmp_path / "Group_A.csv"
    bse1000.write_text(
        "Constituents,Symbol,Macro-Economic Sector\nABB INDIA LIMITED,500002,Industrials\n",
        encoding="utf-8",
    )
    group_a.write_text(
        "Security Code,Issuer Name,Security Id,Security Name,Status,Group,Face Value,ISIN No,Instrument\n"
        "500002,ABB India Limited,ABB,ABB India Ltd,Active,A,2,INE117A01022,Equity,,,,\n",
        encoding="utf-8",
    )
    rows = load_and_deduplicate_universes([str(bse1000), str(group_a)])
    assert len(rows) == 1
    assert rows[0].symbol == "500002"
    assert rows[0].security_id == "ABB"
    assert rows[0].ticker == "ABB.BO"
    assert rows[0].ticker_candidates == ("ABB.BO", "ABB.NS", "500002.BO")


def test_isin_is_never_used_as_yahoo_ticker(tmp_path):
    p = tmp_path / "Group_A.csv"
    p.write_text(
        "Security Code,Issuer Name,Security Id,Security Name,Status,Group,Face Value,ISIN No,Instrument\n"
        "500002,ABB India Limited,INE117A01022,ABB India Ltd,Active,A,2,INE117A01022,Equity\n",
        encoding="utf-8",
    )
    rows = load_universe_file(str(p))
    assert rows[0].security_id == ""
    assert rows[0].ticker == "500002.BO"
    assert all("INE117A01022" not in ticker for ticker in rows[0].ticker_candidates)


def test_history_download_falls_back_from_bse_id_to_nse_id(monkeypatch, tmp_path):
    from dataclasses import replace
    import early_swing_scanner as scanner

    idx = pd.bdate_range("2025-01-01", periods=140)
    close = np.linspace(100, 120, len(idx))
    valid = pd.DataFrame({
        "Open": close - 0.2,
        "High": close + 0.5,
        "Low": close - 0.5,
        "Close": close,
        "Volume": np.full(len(idx), 250000),
    }, index=idx)
    calls = []

    def fake_download(*, tickers, **kwargs):
        requested = [tickers] if isinstance(tickers, str) else list(tickers)
        calls.append(requested)
        if requested == ["ABB.NS"]:
            return valid.copy()
        return pd.DataFrame()

    from types import SimpleNamespace
    monkeypatch.setattr(scanner, "_get_yfinance", lambda: SimpleNamespace(download=fake_download))
    rec = UniverseRecord(
        symbol="500002", ticker="ABB.BO", company_name="ABB India Ltd",
        exchange="BSE", bse_group="A", sector="Industrials",
        source_universes=["BSE Group A"], security_id="ABB",
    )
    config = replace(
        DEFAULT_EARLY_SWING_CONFIG,
        cache_directory=str(tmp_path / "cache"),
        batch_size=10,
        fetch_retries=1,
        batch_pause_seconds=0,
    )
    histories = scanner.fetch_histories([rec], config, use_cache=False)
    assert rec.stable_key in histories
    assert histories[rec.stable_key].attrs["source_ticker"] == "ABB.NS"
    assert calls == [["ABB.BO"], ["ABB.NS"]]


def test_history_cache_is_keyed_by_stable_security_not_attempted_ticker(monkeypatch, tmp_path):
    from dataclasses import replace
    import early_swing_scanner as scanner

    idx = pd.bdate_range("2025-01-01", periods=140)
    close = np.linspace(100, 120, len(idx))
    valid = pd.DataFrame({
        "Open": close - 0.2, "High": close + 0.5, "Low": close - 0.5,
        "Close": close, "Volume": np.full(len(idx), 250000),
    }, index=idx)
    counter = {"calls": 0}

    def fake_download(*, tickers, **kwargs):
        counter["calls"] += 1
        requested = [tickers] if isinstance(tickers, str) else list(tickers)
        return valid.copy() if requested == ["ABB.NS"] else pd.DataFrame()

    from types import SimpleNamespace
    monkeypatch.setattr(scanner, "_get_yfinance", lambda: SimpleNamespace(download=fake_download))
    rec = UniverseRecord(
        symbol="500002", ticker="ABB.BO", company_name="ABB India Ltd",
        exchange="BSE", bse_group="A", sector="Industrials",
        source_universes=["BSE Group A"], security_id="ABB",
    )
    config = replace(
        DEFAULT_EARLY_SWING_CONFIG,
        cache_directory=str(tmp_path / "cache"), batch_size=10,
        fetch_retries=1, batch_pause_seconds=0,
    )
    first = scanner.fetch_histories([rec], config, use_cache=True)
    first_calls = counter["calls"]
    second = scanner.fetch_histories([rec], config, use_cache=True)
    assert first[rec.stable_key].attrs["source_ticker"] == "ABB.NS"
    assert second[rec.stable_key].attrs["source_ticker"] == "ABB.NS"
    assert counter["calls"] == first_calls
