# Repository-wide ticker repair

This package is the **full repository**, with the ticker-resolution fixes already applied.

## Replace the repository with this package
Do not combine the two earlier repair ZIPs. Use this package as the single source of truth.

### Corrected files
- `Scanner/afternoon_fresh_move_scanner.py`
- `Scanner/afternoon_fresh_move_scanner.baseline.py`
- `Scanner/ticker_resolver.py` (new)
- `Scanner/afternoon_config.py`
- `Scanner/market_scanner.py`
- `Scanner/bse_gainer_scanner.py`

### Intentionally unchanged ticker-aware scanners
- `group_ab_scanner.py`
- `early_swing_universe.py`
- `early_swing_scanner.py`
- `early_swing_models.py`
- `market_scanner_UPDATED_backup.py`
- `resolve_bse_symbols.py`

These already implement fallback/resolution logic or intentionally use NSE-only symbols.

## Important
`afternoon_fresh_move_scanner.py` is the version from the first ticker repair and is **not** the smaller copy from the repository-wide audit ZIP. The two earlier ZIPs should therefore NOT be mixed manually.

The baseline file is made consistent with the repaired afternoon implementation so that accidentally invoking the backup does not restore the old broken ticker behavior.

## Validation
- Python `compileall`: passed for the complete package.
- Ticker/early-swing tests: **30 passed**.
- Full pytest collection could not run in this environment because `yfinance` is not installed here; that is an environment dependency issue, not a source compilation error.

## What the fix does
The scanner treats BSE/NSE Yahoo symbols as data-source candidates, not as immutable company identity. A failed `.BO` lookup can fall back to `.NS` and other valid candidates, and the actual successful `source_ticker` is propagated downstream instead of reconstructing a suffix later.
