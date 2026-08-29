from ticker_resolver import candidate_tickers

def test_bare_primary_is_not_sent_raw_to_yahoo():
    stock={"ticker":"AMJLAND","security_id":"AMJLAND","symbol":"AMJLAND"}
    c=candidate_tickers(stock)
    assert "AMJLAND" not in c
    assert c[:2] == ("AMJLAND.NS","AMJLAND.BO")

def test_existing_suffix_is_preserved():
    stock={"ticker":"AMJLAND.BO","security_id":"AMJLAND"}
    assert candidate_tickers(stock)[0] == "AMJLAND.BO"

def test_isin_is_not_a_yahoo_candidate():
    stock={"ticker":"INE123A01016","security_id":"INE123A01016"}
    assert not candidate_tickers(stock)
