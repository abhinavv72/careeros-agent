@echo off
cd /d "%~dp0"
python jobpilot.py doctor
if errorlevel 1 goto finished
python jobpilot.py run --apply %*
:finished
pause
