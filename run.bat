@echo off
setlocal EnableExtensions
cd /d "%~dp0"
chcp 65001 >nul 2>&1
set "PYTHONUTF8=1"

if exist ".venv\Scripts\python.exe" (
    set "BOT_PYTHON=.venv\Scripts\python.exe"
) else (
    set "BOT_PYTHON=python"
)

echo ================================================
echo   Crypto Trading Bot for Bybit
echo ================================================
echo.
echo Starting: %BOT_PYTHON% main.py %*
echo.

"%BOT_PYTHON%" main.py %*
set "EXIT_CODE=%ERRORLEVEL%"

echo.
echo Bot stopped with exit code %EXIT_CODE%.
endlocal & exit /b %EXIT_CODE%
