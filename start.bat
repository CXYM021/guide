@echo off
chcp 65001 >nul
cd /d "%~dp0"
echo Starting proficiency-web (this computer only)...
echo Open http://127.0.0.1:8765/ in your browser.
echo For phones / other computers on the same WiFi: CLOSE this and run start_lan.bat
echo (it listens on all network interfaces and prints the real LAN address).
echo Keep this window open. Close it to stop the server.
echo.
python server.py
if errorlevel 1 (
  echo.
  echo Start failed. Python 3.10+ is required and must be on PATH.
)
pause
