@echo off
REM Dense near-tipoff odds collector
REM Runs daily from 3 PM ET, collects at multiple offsets before each game

cd /d "C:\Users\thegr\OneDrive\Desktop\nba data ver 2"
.\.venv\Scripts\python.exe scripts\dense_collector.py --books betmgm,draftkings,fanduel,pinnacle --stats pts,ast,reb,pra,fg3m,stl,blk --max-requests 2000 >> "data\logs\dense_collector_task.log" 2>&1
