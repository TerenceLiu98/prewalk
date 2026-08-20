#!/usr/bin/env bash
set -euo pipefail

if [ -n "${PLUGIN_ROOT:-}" ]; then
  ROOT="$PLUGIN_ROOT"
else
  ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
fi

exec python3 "$ROOT/hooks/executor_router.py"
