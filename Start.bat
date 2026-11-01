@echo off
cd /d "%~dp0"
start "" cmd /c "timeout /t 3 >nul & start chrome http://127.0.0.1:8000/#dashboard"
python run.py
pause