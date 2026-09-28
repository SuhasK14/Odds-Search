@echo off
REM Price-history snapshot for Odds-Search. Runs unattended from Task Scheduler.
REM Free: DraftKings and FanDuel only, never the Odds API.
REM Add sports by appending more lines in the SPORTS list below.

setlocal
set "PY=%LOCALAPPDATA%\Programs\Python\Python312\python.exe"
set "REPO=%~dp0"
set "PYTHONIOENCODING=utf-8"
set "SPORTS=nfl"

if not exist "%REPO%snapshots" mkdir "%REPO%snapshots"

cd /d "%REPO%"
for %%S in (%SPORTS%) do (
  echo. >> "%REPO%snapshots\snapshot.log"
  echo ==== %date% %time%  %%S ==== >> "%REPO%snapshots\snapshot.log"
  "%PY%" "%REPO%snapshot.py" --sport %%S >> "%REPO%snapshots\snapshot.log" 2>&1
)
endlocal
