@echo off
setlocal EnableExtensions EnableDelayedExpansion
cd /d "%~dp0"
chcp 65001 >nul 2>&1
set "PYTHONUTF8=1"
set "PIP_DISABLE_PIP_VERSION_CHECK=1"

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
        goto :failed
    )
    !BOOTSTRAP_PY! -c "import sys; raise SystemExit(0 if sys.version_info >= (3, 10) else 1)" >nul 2>&1
    if errorlevel 1 (
        echo [ERROR] Python 3.10 or newer is required.
        goto :failed
    )
    echo [SETUP] Creating local .venv...
    !BOOTSTRAP_PY! -m venv .venv
    if errorlevel 1 (
        echo [ERROR] Could not create .venv.
        goto :failed
    )
)

"%PYTHON_CMD%" -c "import sys; raise SystemExit(0 if sys.version_info >= (3, 10) else 1)" >nul 2>&1
if errorlevel 1 (
    echo [ERROR] .venv uses Python older than 3.10.
    echo [ACTION] Recreate .venv with Python 3.10 or newer.
    goto :failed
)

if not exist ".env" (
    if not exist ".env.example" (
        echo [ERROR] Neither .env nor .env.example was found.
        goto :failed
    )
    copy /y ".env.example" ".env" >nul
    echo [SETUP] Created .env from .env.example.
    echo [ACTION] Fill in the keys in .env, then run start.bat again.
    goto :failed
)

"%PYTHON_CMD%" -c "import aiogram, dotenv, loguru, matplotlib, numpy, openai, requests; from aiogram.types import InputRichMessage; from importlib.metadata import version; from packaging.version import Version; aiogram_version = Version(version('aiogram')); matplotlib_version = Version(version('matplotlib')); assert Version('3.30.0') <= aiogram_version < Version('4'); assert Version('3.10.8') <= matplotlib_version < Version('4')" >nul 2>&1
if errorlevel 1 (
    echo [SETUP] Installing dependencies into .venv...
    "%PYTHON_CMD%" -m pip install -r requirements.txt
    if errorlevel 1 (
        echo [ERROR] Dependency installation failed.
        goto :failed
    )
)

echo [START] Telegram bot
"%PYTHON_CMD%" main.py telegram %*
set "EXIT_CODE=%ERRORLEVEL%"
if not "%EXIT_CODE%"=="0" (
    echo.
    echo [ERROR] Bot stopped with exit code %EXIT_CODE%.
    pause
)
endlocal & exit /b %EXIT_CODE%

:failed
echo.
pause
endlocal & exit /b 1
