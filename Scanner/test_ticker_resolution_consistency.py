"""Regression tests for canonical ticker candidate consistency."""
from ticker_resolver import candidate_tickers, exchange_tickers

def test_bse_record_prefers_nse_then_bse_then_numeric():
    stock = {
        "security_code": "500002",
        "security_id": "ABB",
        "symbol": "500002",
        "ticker": "ABB.NS",
        "alternate_tickers": ["ABB.BO", "500002.BO"],
    }
    assert candidate_tickers(stock)[:3] == ("ABB.NS", "ABB.BO", "500002.BO")
    ex = exchange_tickers(stock)
    assert ex["nse"] == "ABB.NS"
    assert ex["bse"] == "ABB.BO"

def test_nse_record_keeps_nse_primary_and_bse_fallback():
    stock = {
        "security_code": "",
        "security_id": "TCS",
        "symbol": "TCS",
        "ticker": "TCS.NS",
        "alternate_tickers": ["TCS.BO"],
    }
    assert candidate_tickers(stock)[:2] == ("TCS.NS", "TCS.BO")

def test_bse_fund_unit_is_not_admitted_as_company_equity():
    from ticker_resolver import is_equity_company
    assert not is_equity_company({
        "security_code": "543146",
        "security_id": "08MPR",
        "isin": "INF204KB12T3",
        "instrument": "Equity",
        "name": "Nippon India Equity Savings Fund - Segregated Portfolio",
    })


def test_real_bse_company_equity_is_admitted():
    from ticker_resolver import is_equity_company
    assert is_equity_company({
        "security_code": "500002",
        "security_id": "ABB",
        "isin": "INE117A01022",
        "instrument": "Equity",
        "name": "ABB India Limited",
    })


def test_candidates_do_not_trust_bare_ticker_as_yahoo_symbol():
    from ticker_resolver import candidate_tickers
    stock = {
        "security_code": "543146",
        "security_id": "08MPR",
        "isin": "INF204KB12T3",
        "ticker": "08MPR",
    }
    assert all(not t.startswith("08MPR") for t in candidate_tickers(stock))
