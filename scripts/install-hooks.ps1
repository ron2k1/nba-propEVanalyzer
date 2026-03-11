# Install git hooks for this repo (PowerShell).
# Usage: powershell scripts/install-hooks.ps1 [-Force]

param([switch]$Force)

$ErrorActionPreference = "Stop"

$RepoRoot = Split-Path -Parent (Split-Path -Parent $MyInvocation.MyCommand.Path)
$HooksDir = Join-Path $RepoRoot ".git\hooks"
$Target = Join-Path $HooksDir "pre-commit"

if ((Test-Path $Target) -and -not $Force) {
    $content = Get-Content $Target -Raw -ErrorAction SilentlyContinue
    if ($content -match "quality_gate\.py") {
        Write-Host "Pre-commit hook already installed (ours). Use -Force to overwrite."
        exit 0
    } else {
        Write-Error ".git/hooks/pre-commit already exists and was not installed by this script. Inspect it manually, then re-run with -Force to overwrite."
        exit 1
    }
}

$hookContent = @'
#!/usr/bin/env bash
# Pre-commit hook: runs quality_gate.py (default checks only).
# Blocks commit on failure. Bypass with: git commit --no-verify

set -euo pipefail

REPO_ROOT="$(git rev-parse --show-toplevel)"

if [ -f "$REPO_ROOT/.venv/Scripts/python.exe" ]; then
    PYTHON=("$REPO_ROOT/.venv/Scripts/python.exe")
elif [ -f "$REPO_ROOT/.venv/bin/python" ]; then
    PYTHON=("$REPO_ROOT/.venv/bin/python")
elif command -v python3 &>/dev/null; then
    PYTHON=(python3)
elif command -v python &>/dev/null; then
    PYTHON=(python)
elif command -v py &>/dev/null; then
    PYTHON=(py -3)
else
    echo "ERROR: No python interpreter found. Activate .venv or install Python."
    exit 1
fi

echo "Running quality gate..."
if ! "${PYTHON[@]}" "$REPO_ROOT/scripts/quality_gate.py"; then
    echo ""
    echo "COMMIT BLOCKED: quality gate failed."
    echo "Fix the issues above, then try again."
    echo "To bypass: git commit --no-verify"
    exit 1
fi

echo "Quality gate passed."
'@

# Write with Unix line endings (Git hooks run via Git Bash even on Windows)
$utf8NoBom = New-Object System.Text.UTF8Encoding $false
[System.IO.File]::WriteAllText($Target, ($hookContent -replace "`r`n", "`n"), $utf8NoBom)

Write-Host "Installed pre-commit hook."
