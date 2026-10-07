@echo off
chcp 65001 > nul
title Antigravity Telegram Bot
cd /d "%~dp0"

echo ===================================================
echo           Antigravity Telegram Bot
echo ===================================================
echo.

set "PYTHON_EXE=%LOCALAPPDATA%\Programs\Python\Python313\python.exe"

if not exist "%PYTHON_EXE%" (
    set "PYTHON_EXE=C:\Users\Chatty\AppData\Local\Programs\Python\Python313\python.exe"
)

if not exist "%PYTHON_EXE%" (
    set "PYTHON_EXE=python"
)

"%PYTHON_EXE%" bot.py

if %ERRORLEVEL% neq 0 (
    echo.
    echo [!] Бот завершил работу с ошибкой.
    pause
)
