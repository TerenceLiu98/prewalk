#!/usr/bin/env bash
# Codex prewalk hook wrapper — PreToolUse event (edit gate on apply_patch).
# Codex runs hook `command`s with cwd = plugin root, and sets PLUGIN_ROOT.
set -euo pipefail

if [ -n "${PLUGIN_ROOT:-}" ]; then
  ROOT="$PLUGIN_ROOT"
else
  ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
fi

exec python3 "$ROOT/hooks/edit_gate.py"
