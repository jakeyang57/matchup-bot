@echo off
REM Double-click to run Matchup Bot now. Task Scheduler calls this with --scheduled.
cd /d "%~dp0"
set "PY=python"
where py >nul 2>nul && set "PY=py -3"
%PY% matchup_bot.py %*
if /I not "%~1"=="--scheduled" (
  echo.
  pause
)
