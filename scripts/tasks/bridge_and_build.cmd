@echo off
REM Nightly fallback: bridge JSONL snapshots to OddsStore + derive closing lines
REM Ensures JSONL -> SQLite conversion happens daily even if dense collector exits early

cd /d "C:\Users\thegr\OneDrive\Desktop\nba data ver 2"
.\.venv\Scripts\python.exe nba_mod.py line_bridge >> "data\logs\bridge_and_build.log" 2>&1
.\.venv\Scripts\python.exe nba_mod.py odds_build_closes >> "data\logs\bridge_and_build.log" 2>&1
