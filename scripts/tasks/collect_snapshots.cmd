@echo off
REM Collect odds snapshots (collect-only, no sweep/best_today)
REM Runs every 2 hours to accumulate closing-line data

cd /d "C:\Users\thegr\OneDrive\Desktop\nba data ver 2"
.\.venv\Scripts\python.exe scripts\scheduled_pipeline.py --collect-only >> "data\logs\task_collect.log" 2>&1
