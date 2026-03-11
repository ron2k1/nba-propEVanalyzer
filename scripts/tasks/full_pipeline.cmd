@echo off
REM Full daily pipeline: collect_lines + roster_sweep + best_today
REM Runs once daily at 5 PM before most tipoffs

cd /d "C:\Users\thegr\OneDrive\Desktop\nba data ver 2"
.\.venv\Scripts\python.exe scripts\scheduled_pipeline.py >> "data\logs\task_pipeline.log" 2>&1
