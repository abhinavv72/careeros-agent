@echo off
cd /d "%~dp0"
python -X utf8 agent_launcher.py
if errorlevel 1 pause
