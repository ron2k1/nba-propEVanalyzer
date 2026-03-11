@echo off
REM Morning settlement: paper_settle yesterday + paper_summary (14d window)
REM Runs once daily at 10 AM

cd /d "C:\Users\thegr\OneDrive\Desktop\nba data ver 2"
.\.venv\Scripts\python.exe scripts\scheduled_settle.py >> "data\logs\task_settle.log" 2>&1
