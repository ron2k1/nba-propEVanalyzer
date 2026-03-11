@echo off
REM Injury monitor: poll injury news and alert via Discord for new signals
REM Runs every 2 hours on game days

cd /d "C:\Users\thegr\OneDrive\Desktop\nba data ver 2"
.\.venv\Scripts\python.exe scripts\monitor_injuries.py >> "data\logs\task_monitor_injuries.log" 2>&1
