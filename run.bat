@echo off
setlocal EnableExtensions
cd /d "%~dp0"
chcp 65001 >nul 2>&1
set "PYTHONUTF8=1"

set "PYTHON_CMD=.venv\Scripts\python.exe"
if not exist "%PYTHON_CMD%" (
    set "BOOTSTRAP_PY="
    where py >nul 2>&1
    if not errorlevel 1 set "BOOTSTRAP_PY=py -3"
    if not defined BOOTSTRAP_PY (
        where python >nul 2>&1
        if not errorlevel 1 set "BOOTSTRAP_PY=python"
    )
    if not defined BOOTSTRAP_PY (
        echo [ERROR] Python 3 was not found.
        exit /b 1
    )
    echo [SETUP] Creating local .venv...
    %BOOTSTRAP_PY% -m venv .venv
    if errorlevel 1 (
        echo [ERROR] Could not create .venv.
        exit /b 1
    )
)

if exist "requirements.txt" (
    echo [SETUP] Installing dependencies into .venv...
    set "PIP_DISABLE_PIP_VERSION_CHECK=1"
    "%PYTHON_CMD%" -m pip install -r requirements.txt
    if errorlevel 1 (
        echo [ERROR] Dependency installation failed.
        exit /b 1
    )
)

echo ================================================
echo   Crypto Trading Bot for Bybit
echo ================================================
echo.
echo Starting: %PYTHON_CMD% main.py %*
echo.

"%PYTHON_CMD%" main.py %*
set "EXIT_CODE=%ERRORLEVEL%"

echo.
echo Bot stopped with exit code %EXIT_CODE%.
endlocal & exit /b %EXIT_CODE%
