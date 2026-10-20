@echo off
chcp 65001 >nul
title AI Book Translator Hub - Local Server
echo ===============================================================================
echo   AI BOOK TRANSLATOR HUB - TIM SACH VA DICH TU DONG DA LUONG (EPUB/DOCX/PDF)
echo ===============================================================================
echo.
echo [1/2] Dang khoi dong Backend Server tai http://localhost:8088 ...
cd /d "%~dp0"
start "" "http://localhost:8088"
python -m uvicorn backend.main:app --host 0.0.0.0 --port 8088 --reload
pause
