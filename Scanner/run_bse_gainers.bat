@echo off
setlocal

set PROJECT_DIR=D:\PythonProject\DailyStockScreener
set SCANNER_DIR=%PROJECT_DIR%\Scanner
set PYTHON_EXE=%PROJECT_DIR%\.venv\Scripts\python.exe

cd /d "%SCANNER_DIR%"

echo ============================================== >> bse_gainers_log.txt
echo Run started: %DATE% %TIME% >> bse_gainers_log.txt

"%PYTHON_EXE%" bse_gainer_scanner.py trading_toolkit.xlsx bse_smallcap250.csv >> bse_gainers_log.txt 2>&1

echo Run finished: %DATE% %TIME% >> bse_gainers_log.txt

endlocal
