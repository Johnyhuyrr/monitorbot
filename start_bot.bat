@echo off
title Zade Meadows Monitor
cd /d "%~dp0"
chcp 65001 >nul

:loop
echo [%date% %time%] Starting bot...
python bot.py
echo [%date% %time%] Bot exited with code %errorlevel%. Restarting in 15 seconds...
timeout /t 15 /nobreak >nul
goto loop
