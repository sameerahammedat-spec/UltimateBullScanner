# Production ticker/data-source fix — 2026-08-29

## Diagnosis

The 14:36 afternoon run still attempted BSE `.BO` symbols first. The failed examples are genuine listed companies with NSE symbols, so these messages are not evidence that the companies are delisted. Examples verified externally include:

- AMJLAND.NS — current price history exists.
- CURAA.NS — current price history exists.
- DICIND.NS — current price history exists.
- GUJRAFFIA.NS — current price history exists.
- GANGESSECU.NS — Yahoo lookup identifies it as an NSE equity.
- NAHARCAP.NS — current NSE quote/data exists.
- RHL.NS — current price history exists.
- SABEVENTS.NS — current price history exists and Yahoo lists both BSE/NSE symbols.
- SRGHFL.NS — current NSE quote/data exists.
- SURYALA.NS and THAKDEV.NS — current NSE listings exist.
- ZENITHEXPO.NS — historical daily prices exist.

Low volume is a separate condition. It can result in few/no intraday 5-minute bars, but it does not explain Yahoo's `possibly delisted; no price data found (period=1d)` response for the `.BO` symbol. The scanner must therefore resolve the data ticker first, then apply liquidity/bar-count rules.

## Production changes

1. Afternoon company universe now prefers `<Security Id>.NS` for Yahoo price history.
2. `<Security Id>.BO` remains the first deterministic fallback.
3. Numeric `<Security Code>.BO` is only a final legacy fallback.
4. If all deterministic candidates fail, Yahoo symbol search by company name is attempted only for the small failed set.
5. A candidate is accepted only if real OHLC data exists; a non-empty all-NaN yfinance frame is treated as a failed download.
6. The actual resolved Yahoo ticker is stored in `source_ticker`/`resolved_ticker` and is used downstream; the code never reconstructs `.BO`/`.NS` from the BSE identity.
7. When intraday resolves to an alternate exchange ticker, daily data is reloaded from that same ticker when possible to avoid cross-exchange OHLC/volume mismatches.
8. CSV loading explicitly selects the declared columns, eliminating the trailing-comma ParserWarning without dropping valid company rows.
9. Added `ticker_coverage_diagnostic.py` for a full-universe static Yahoo coverage test using short daily history.

## Recommended verification

Run from `Scanner`:

    python -m compileall -q .
    python ticker_coverage_diagnostic.py
    python afternoon_fresh_move_scanner.py --once

The coverage diagnostic is intentionally daily-only. It answers whether Yahoo exposes the ticker before the expensive 5-minute scanner is run.

Do not commit or overwrite your local `.env` from this package. Keep your existing credentials.
