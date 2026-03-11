@echo off
cd /d "C:\Users\thegr\OneDrive\Desktop\nba data ver 2\scripts\tasks"

echo Registering NBA scheduled tasks...
echo.

schtasks /create /tn "NBASnapshotCollection" /xml "%~dp0task_collect.xml" /f
if %ERRORLEVEL% EQU 0 (echo [OK] NBASnapshotCollection) else (echo [FAIL] NBASnapshotCollection)

schtasks /create /tn "NBAFullPipeline" /xml "%~dp0task_pipeline.xml" /f
if %ERRORLEVEL% EQU 0 (echo [OK] NBAFullPipeline) else (echo [FAIL] NBAFullPipeline)

schtasks /create /tn "NBAMorningSettle" /xml "%~dp0task_settle.xml" /f
if %ERRORLEVEL% EQU 0 (echo [OK] NBAMorningSettle) else (echo [FAIL] NBAMorningSettle)

echo.
echo Checking registered tasks:
schtasks /query /tn "NBASnapshotCollection" /v /fo list 2>nul | findstr "Task\|Status\|Next"
schtasks /query /tn "NBAFullPipeline" /v /fo list 2>nul | findstr "Task\|Status\|Next"
schtasks /query /tn "NBAMorningSettle" /v /fo list 2>nul | findstr "Task\|Status\|Next"
echo.
echo Done. To run now: schtasks /run /tn NBASnapshotCollection
pause
