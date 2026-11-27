@echo off
cd /d "%~dp0"
python jobpilot.py run %*
pause
