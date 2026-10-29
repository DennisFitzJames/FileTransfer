@echo off
setlocal
cd /d "%~dp0"
if exist "%~dp0venv\Scripts\python.exe" (
    "%~dp0venv\Scripts\python.exe" app\scheduled_datahub_refresh.py --run
) else (
    py -3 app\scheduled_datahub_refresh.py --run
)
exit /b %errorlevel%
