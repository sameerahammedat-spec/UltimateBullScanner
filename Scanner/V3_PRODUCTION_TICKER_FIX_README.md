# UltimateBullScanner V3 Production Ticker/Data Fix

This package is a full repository replacement, not a patch overlay.

Key fixes:
- Afternoon universe prefers NSE (.NS), with BSE (.BO) and BSE code fallback.
- Batch downloads now validate actual OHLC data, not merely non-empty DataFrames.
- Missing primary tickers are retried through alternate exchange symbols.
- Actual resolved ticker is recorded on the stock record.
- Intraday availability is treated separately from daily ticker validity.
- Low volume is never treated as a ticker/API failure; liquidity filters remain scanner logic.
- CSV reader trims only extra trailing fields, eliminating the BSE trailing-column ParserWarning.
- Added ticker_coverage_diagnostic.py for production coverage verification.

Important:
A Yahoo message "possibly delisted; no price data found" is not proof of delisting.
The V3 scanner classifies missing intraday history separately and tries alternate
exchange symbols before giving up.

Install dependencies from requirements.txt and keep your local .env/credentials.
