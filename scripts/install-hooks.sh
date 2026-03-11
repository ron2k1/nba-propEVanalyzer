#!/usr/bin/env bash
# Install git hooks for this repo.
# Usage: bash scripts/install-hooks.sh [--force]
# Requires Git Bash on Windows.

set -euo pipefail

REPO_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
HOOKS_DIR="$REPO_ROOT/.git/hooks"
TARGET="$HOOKS_DIR/pre-commit"
FORCE=0

for arg in "$@"; do
    [ "$arg" = "--force" ] && FORCE=1
done

if [ -f "$TARGET" ] && [ "$FORCE" -eq 0 ]; then
    if grep -q "quality_gate.py" "$TARGET" 2>/dev/null; then
        echo "Pre-commit hook already installed (ours). Use --force to overwrite."
        exit 0
    else
        echo "ERROR: .git/hooks/pre-commit already exists and was not installed by this script."
        echo "Inspect it manually, then re-run with --force to overwrite."
        exit 1
    fi
fi

cat > "$TARGET" << 'HOOK'
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
HOOK

chmod +x "$TARGET"
echo "Installed pre-commit hook."
