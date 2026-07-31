from __future__ import annotations

import importlib.util
import sys
import types
from pathlib import Path

import numpy as np
import pandas as pd

MODULE_PATH = Path(__file__).resolve().with_name('group_ab_scanner.py')
if not MODULE_PATH.exists():
    raise FileNotFoundError(f'Expected active scanner beside test file: {MODULE_PATH}')
spec = importlib.util.spec_from_file_location('group_ab_scanner_ticker_fixed', MODULE_PATH)
module = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = module
assert spec.loader is not None
spec.loader.exec_module(module)


def make_grouped_history(tickers, periods=80):
    dates = pd.date_range('2026-01-01', periods=periods, freq='B')
    fields = ['Open', 'High', 'Low', 'Close', 'Volume']
    columns = pd.MultiIndex.from_product([tickers, fields], names=['Ticker', 'Price'])
    data = np.zeros((periods, len(columns)), dtype=float)
    for i, (_, field) in enumerate(columns):
        data[:, i] = 250000 if field == 'Volume' else np.linspace(100, 125, periods)
    return pd.DataFrame(data, index=dates, columns=columns)


def test_security_id_is_primary_yahoo_ticker():
    record = module.UniverseRecord(
        symbol='500002', security_id='ABB', name='ABB India Ltd',
        sector='', group='A', sources=('Group A',),
    )
    assert record.yahoo_ticker == 'ABB.BO'
    assert record.yahoo_fallback_tickers == ('ABB.NS', '500002.BO')


def test_special_security_id_is_preserved_safely():
    record = module.UniverseRecord(
        symbol='500520', security_id='M&M', name='Mahindra & Mahindra Ltd',
        sector='', group='A', sources=('Group A',),
    )
    assert record.yahoo_ticker == 'M&M.BO'


def test_download_uses_bse_id_then_nse_fallback(monkeypatch):
    records = [
        module.UniverseRecord('500002', 'ABB', 'ABB India Ltd', '', 'A', ('Group A',)),
        module.UniverseRecord('500003', 'AEGISLOG', 'Aegis Logistics Ltd', '', 'A', ('Group A',)),
    ]
    calls = []

    def fake_download(*, tickers, **kwargs):
        tickers_list = tickers if isinstance(tickers, list) else [tickers]
        calls.append(list(tickers_list))
        # Primary BSE-ID pass resolves only AEGISLOG.BO.
        if 'AEGISLOG.BO' in tickers_list:
            return make_grouped_history(['AEGISLOG.BO'])
        # NSE fallback resolves ABB.NS.
        if 'ABB.NS' in tickers_list:
            return make_grouped_history(['ABB.NS'])
        return pd.DataFrame()

    fake_yf = types.SimpleNamespace(download=fake_download)
    monkeypatch.setitem(sys.modules, 'yfinance', fake_yf)

    histories = module.download_histories(
        records, period='1y', batch_size=80, workers=1, use_cache=False,
    )

    assert set(histories) == {'500002', '500003'}
    assert histories['500003'].attrs['source_ticker'] == 'AEGISLOG.BO'
    assert histories['500002'].attrs['source_ticker'] == 'ABB.NS'
    assert calls[0] == ['ABB.BO', 'AEGISLOG.BO']
    assert calls[1] == ['ABB.NS']
    assert all('500002.BO' not in call for call in calls), 'numeric fallback should not run after resolution'


def test_analyze_history_reports_resolved_ticker():
    dates = pd.date_range('2025-01-01', periods=252, freq='B')
    close = pd.Series(np.linspace(100, 150, len(dates)), index=dates)
    frame = pd.DataFrame({
        'Open': close * 0.998,
        'High': close * 1.01,
        'Low': close * 0.99,
        'Close': close,
        'Volume': np.full(len(dates), 250000.0),
    }, index=dates)
    frame.attrs['source_ticker'] = 'ABB.NS'
    record = module.UniverseRecord('500002', 'ABB', 'ABB India Ltd', '', 'A', ('Group A',))
    result = module.analyze_history(record, frame, benchmark_1m=1.0, benchmark_3m=2.0)
    assert result is not None
    assert result['ticker'] == 'ABB.NS'
    assert result['data_exchange'] == 'NSE fallback'
