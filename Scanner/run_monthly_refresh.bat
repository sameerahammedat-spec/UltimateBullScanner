@echo off
setlocal

set PROJECT_DIR=D:\PythonProject\DailyStockScreener
set SCANNER_DIR=%PROJECT_DIR%\Scanner
set PYTHON_EXE=%PROJECT_DIR%\.venv\Scripts\python.exe

cd /d "%SCANNER_DIR%"

echo ============================================== >> monthly_refresh_log.txt
echo Run started: %DATE% %TIME% >> monthly_refresh_log.txt

"%PYTHON_EXE%" refresh_universe.py >> monthly_refresh_log.txt 2>&1

echo Run finished: %DATE% %TIME% >> monthly_refresh_log.txt
echo Reminder: bse_smallcap250.csv is NOT auto-refreshed - BSE blocks scripted downloads. >> monthly_refresh_log.txt
echo See GETTING_BSE_SMALLCAP_LIST.md if it's been a while since you last updated it. >> monthly_refresh_log.txt

endlocal
