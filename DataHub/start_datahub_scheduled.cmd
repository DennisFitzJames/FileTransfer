@echo off
setlocal
cd /d "%~dp0"
if not exist "logs" mkdir "logs"
echo [%date% %time%] Starting Data Hub controller via Windows Task Scheduler>>"logs\controller_session.log"
if exist "%~dp0venv\Scripts\python.exe" (
    "%~dp0venv\Scripts\python.exe" -u app.py >>"logs\controller_session.log" 2>&1
) else (
    py -3 -u app.py >>"logs\controller_session.log" 2>&1
)
echo [%date% %time%] Controller stopped (code=%errorlevel%)>>"logs\controller_session.log"
exit /b %errorlevel%
