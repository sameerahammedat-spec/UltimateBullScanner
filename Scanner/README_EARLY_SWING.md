# Early Swing Rally Scanner — Phase 1

This release adds an **independent** scanner. It does not modify or delete any
existing NSE, BSE 1000, Group A/B, BSE Gainer or Explosive Move feature.

## What it detects

- Rally initiation one or two completed sessions ago.
- Breakout from a 10–30-session base.
- Inside-bar and NR7 breakout support.
- Breakout volume and second-day follow-through.
- EMA/SMA trend, RSI, MACD, ADX and ATR.
- Relative strength against the configured benchmark.
- Extension risk, false-breakout risk and remaining resistance space.
- Entry zone, technical/ATR stop, targets, reward-to-risk and position size.

The **Rules Score is not a probability**. Delivery data is shown as
`NOT_AVAILABLE` when it is not supplied; no delivery value is fabricated.

## Files to copy into `Scanner`

Copy all `early_swing_*.py` files into:

`D:\PythonProject\DailyStockScreener\Scanner`

Keep them beside `market_scanner.py` and the other scanner scripts.

## Install

```powershell
python -m pip install pandas numpy openpyxl yfinance pytest
```

Your existing BSE 1000 pattern scanner may separately need:

```powershell
python -m pip install scipy opencv-python-headless
```

## Test

```powershell
python -m pytest test_early_swing.py -v
```

## Safe five-symbol test

Close the Excel workbook, then run:

```powershell
python early_swing_scanner.py trading_toolkit_test.xlsx bse1000_clean.csv Group_A.csv Group_B.csv --max-symbols 5 --no-cache
```

## Full run

```powershell
python early_swing_scanner.py trading_toolkit.xlsx bse1000_clean.csv Group_A.csv Group_B.csv
```

The scanner deduplicates a stock appearing in more than one universe and writes a
dated sheet such as `EarlySwing_31Jul26`.

## Optional labelled sources

```powershell
python early_swing_scanner.py trading_toolkit.xlsx `
  "BSE 1000=bse1000_clean.csv" `
  "BSE Group A=Group_A.csv" `
  "BSE Group B=Group_B.csv"
```

A CSV exported from the gainer workflow can be added as another argument:

```powershell
python early_swing_scanner.py trading_toolkit.xlsx bse1000_clean.csv Group_A.csv Group_B.csv "BSE Gainer=today_gainers.csv"
```

## Output sections

- Actionable Early Swing Setups
- Strong Setups — Wait for Pullback
- Early Setups — Watch / Need Confirmation
- Rejected — Extended Rallies
- Rejected — False Breakout Risk
- Rejected — Poor Risk/Reward
- Insufficient / Stale Data
- Other Rejections
- Market Dashboard

## Important limitations

- Daily OHLCV only; no same-time intraday volume.
- Delivery and bid-ask spread are not fabricated.
- Current-universe backtests may contain survivorship bias.
- Phase 1 has a transparent rules score. A calibrated probability requires a
  pooled, chronological out-of-sample dataset and is not claimed here.

### Existing BSE Gainer results

By default, the scanner also reads the first table from the latest `BSEGain_*`
sheet already present in the workbook, merges those securities into the universe,
and retains `BSE Gainer` in the Sources column. Disable this only when needed:

```powershell
python early_swing_scanner.py trading_toolkit.xlsx bse1000_clean.csv Group_A.csv Group_B.csv --no-gainers
```
