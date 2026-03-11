@echo off
REM Dead-man health check: alert via Discord if any scheduled task is stale
REM Runs every 4 hours

cd /d "C:\Users\thegr\OneDrive\Desktop\nba data ver 2"
.\.venv\Scripts\python.exe scripts\scheduled_deadman.py >> "data\logs\task_deadman.log" 2>&1
