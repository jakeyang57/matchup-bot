@echo off
REM Creates a Windows scheduled task that runs Matchup Bot every day at 10:00 AM.
REM To change the time, edit 10:00 below and run this file again.
cd /d "%~dp0"

schtasks /Create /F /TN "Matchup Bot" /SC DAILY /ST 10:00 /TR "\"%~dp0run_bot.bat\" --scheduled"
if errorlevel 1 (
  echo.
  echo Could not create the task. Try right-clicking this file and choosing "Run as administrator".
  pause
  exit /b 1
)

REM If the PC was off or asleep at 10:00, run as soon as it's back on.
powershell -NoProfile -Command "$s = New-ScheduledTaskSettingsSet -StartWhenAvailable -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries; Set-ScheduledTask -TaskName 'Matchup Bot' -Settings $s | Out-Null" 2>nul

echo.
echo Done. Matchup Bot will run every day at 10:00 AM.
echo To remove it: open Task Scheduler, find "Matchup Bot", right-click, Delete.
pause
