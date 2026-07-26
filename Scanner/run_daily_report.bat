@echo off
setlocal

set PROJECT_DIR=D:\PythonProject\DailyStockScreener
set SCANNER_DIR=%PROJECT_DIR%\Scanner
set PYTHON_EXE=%PROJECT_DIR%\.venv\Scripts\python.exe

cd /d "%SCANNER_DIR%"

echo ============================================== >> daily_report_log.txt
echo Run started: %DATE% %TIME% >> daily_report_log.txt

"%PYTHON_EXE%" send_daily_report.py trading_toolkit.xlsx nifty500.csv niftysmallcap250.csv >> daily_report_log.txt 2>&1

echo Run finished: %DATE% %TIME% >> daily_report_log.txt

endlocal
