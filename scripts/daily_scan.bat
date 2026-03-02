@echo off
cd /d "C:\Users\thegr\OneDrive\Desktop\nba data ver 2"
set LOGFILE=data\agent_logs\daily_scan_%DATE:~10,4%-%DATE:~4,2%-%DATE:~7,2%.log
echo [%DATE% %TIME%] Starting daily_scan >> "%LOGFILE%"
".venv\Scripts\python.exe" scripts\nba_agent.py --workflow daily_scan >> "%LOGFILE%" 2>&1
echo [%DATE% %TIME%] Done >> "%LOGFILE%"
