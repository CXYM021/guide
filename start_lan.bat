@echo off
chcp 65001 >nul
cd /d "%~dp0"
set "PROF_HOST=0.0.0.0"
if not defined PROF_INVITE_CODE (
  echo.
  echo WARNING: no invite code. Anyone on this WiFi can register an account.
  echo To set one, run:  set PROF_INVITE_CODE=yourcode
  echo then run this file again from the same window.
  echo.
)
echo Starting proficiency-web for OTHER devices on this network...
echo Use the LAN address printed below on phones / other computers.
echo Windows Firewall may ask to allow Python: check "Private networks" and allow.
echo Keep this window open. Close it to stop the server.
echo.
python server.py
if errorlevel 1 (
  echo.
  echo Start failed. Python 3.10+ is required and must be on PATH.
)
pause
