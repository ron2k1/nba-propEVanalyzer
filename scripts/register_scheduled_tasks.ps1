param(
    [string]$RepoRoot = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path,
    [string]$PythonPath = (Resolve-Path (Join-Path (Resolve-Path (Join-Path $PSScriptRoot "..")).Path ".venv\Scripts\python.exe")).Path
)

$ErrorActionPreference = "Stop"

$pipelineScript = Join-Path $RepoRoot "scripts\scheduled_pipeline.py"
$settleScript = Join-Path $RepoRoot "scripts\scheduled_settle.py"

if (-not (Test-Path $PythonPath)) {
    throw "Python executable not found: $PythonPath"
}

if (-not (Test-Path $pipelineScript)) {
    throw "Pipeline script not found: $pipelineScript"
}

if (-not (Test-Path $settleScript)) {
    throw "Settle script not found: $settleScript"
}

$principal = New-ScheduledTaskPrincipal -UserId $env:USERNAME -LogonType InteractiveToken -RunLevel Highest
$settings = New-ScheduledTaskSettingsSet -StartWhenAvailable -MultipleInstances IgnoreNew -ExecutionTimeLimit (New-TimeSpan -Hours 2)

function Register-NbaTask {
    param(
        [string]$TaskName,
        [string]$ArgumentString,
        [object[]]$Triggers
    )

    $action = New-ScheduledTaskAction -Execute $PythonPath -Argument $ArgumentString -WorkingDirectory $RepoRoot
    Register-ScheduledTask -TaskName $TaskName -Action $action -Trigger $Triggers -Principal $principal -Settings $settings -Force | Out-Null
}

$weekdayTimes = @("12:00PM", "03:00PM", "06:00PM", "08:00PM")
$collectTriggers = foreach ($time in $weekdayTimes) {
    New-ScheduledTaskTrigger -Weekly -WeeksInterval 1 -DaysOfWeek Monday,Tuesday,Wednesday,Thursday,Friday -At $time
}

$collectArgs = "`"$pipelineScript`" --collect-only --books betmgm,draftkings,fanduel --stats pts,ast,reb,pra"
$fullPipelineArgs = "`"$pipelineScript`" --books betmgm,draftkings,fanduel --stats pts,ast,reb,pra --limit 20"
$settleArgs = "`"$settleScript`" --window-days 14"

Register-NbaTask -TaskName "NBA-CollectLines" -ArgumentString $collectArgs -Triggers $collectTriggers
Register-NbaTask -TaskName "NBA-FullPipeline" -ArgumentString $fullPipelineArgs -Triggers @(
    New-ScheduledTaskTrigger -Weekly -WeeksInterval 1 -DaysOfWeek Monday,Tuesday,Wednesday,Thursday,Friday -At "06:30PM"
)
Register-NbaTask -TaskName "NBA-MorningSettle" -ArgumentString $settleArgs -Triggers @(
    New-ScheduledTaskTrigger -Daily -At "10:00AM"
)

Write-Output "Registered tasks:"
Write-Output "  NBA-CollectLines"
Write-Output "  NBA-FullPipeline"
Write-Output "  NBA-MorningSettle"
