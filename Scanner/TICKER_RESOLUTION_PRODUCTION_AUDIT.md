# UltimateBullScanner — canonical ticker resolution repair

This package is the full uploaded repository with the ticker-resolution
inconsistency repaired.

## Key finding

The previous diagnostic and the production afternoon scanner were testing related
but different paths. The diagnostic independently selected NSE/BSE candidates
and tested daily history; the production scanner performed batch 5-minute
downloads and then a separate fallback path. That made the diagnostic's
"ticker works" result insufficient proof that the scanner used the same logic.

## Production fix

`Scanner/ticker_resolver.py` is now the canonical source for:
- candidate ticker generation
- OHLCV validity checking
- per-interval history resolution
- resolved source ticker propagation

`afternoon_fresh_move_scanner.py` uses that resolver for its fallback path and
stores `source_ticker` on successful frames. Deep analysis now refuses to mix
daily data from one exchange with intraday data from another.

`ticker_coverage_diagnostic.py` uses the exact same resolver and reports daily
and 5-minute availability separately:
- DAILY_AND_5M_OK
- DAILY_OK_5M_UNAVAILABLE
- 5M_OK_DAILY_UNAVAILABLE
- NO_USABLE_YAHOO_DATA

This prevents "low volume", "no intraday bars", and transient Yahoo failures
from being mislabelled as ticker failures.

## CSV parsing

The afternoon loader truncates BSE rows to the declared header width, avoiding
the trailing-field ParserWarning caused by malformed/extra CSV fields.

## Validation

All Python files in the uploaded repository compile successfully after these
changes.

Run on the user's Windows environment, where yfinance is installed:
    python -m compileall -q .
    python -m pytest -q
    python ticker_coverage_diagnostic.py
    python afternoon_fresh_move_scanner.py --once

Keep the existing `.env` outside this package and do not overwrite credentials.
