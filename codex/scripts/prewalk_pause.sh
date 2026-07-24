#!/usr/bin/env bash
# Codex prewalk hook wrapper — Stop event (pause detection + state machine).
# Codex runs hook `command`s with cwd = plugin root, and sets PLUGIN_ROOT.
# We resolve the plugin root either way, then run the Python hook.
set -euo pipefail

# PLUGIN_ROOT is set by Codex for plugin-bundled hooks; fall back to the
# script's own location (../.. from scripts/) if unset.
if [ -n "${PLUGIN_ROOT:-}" ]; then
  ROOT="$PLUGIN_ROOT"
else
  ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
fi

exec python3 "$ROOT/hooks/pause_detect.py"
