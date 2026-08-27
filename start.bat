@echo off
chcp 65001 >nul
cd /d "%~dp0"
title Abstraction Bot
uv run python run.py
echo.
echo 종료되었습니다.
pause
