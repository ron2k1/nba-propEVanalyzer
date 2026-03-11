@echo off
REM Line movement monitor: detect significant line moves and alert via Discord
REM Runs every 2 hours (aligned with snapshot collection)

cd /d "C:\Users\thegr\OneDrive\Desktop\nba data ver 2"
.\.venv\Scripts\python.exe scripts\monitor_lines.py >> "data\logs\task_monitor_lines.log" 2>&1
