#!/usr/bin/env python3
"""Prove the v0.4 regression gates detect the released v0.3.0 implementation."""

from __future__ import annotations

import json
from pathlib import Path
import subprocess
import sys


ROOT = Path(__file__).resolve().parents[1]
CONTRACT = json.loads(
    (ROOT / "tests" / "fixtures" / "native_workflow_contracts.json").read_text(encoding="utf-8")
)
BASELINE = CONTRACT["v030_regressions"]["baseline_commit"]


def git_show(path: str) -> str:
    result = subprocess.run(
        ["git", "show", f"{BASELINE}:{path}"],
        cwd=ROOT,
        text=True,
        capture_output=True,
        check=False,
    )
    if result.returncode:
        raise RuntimeError(
            f"cannot read v0.3.0 baseline {BASELINE}:{path}; CI checkout must use fetch-depth: 0"
        )
    return result.stdout


def main() -> int:
    old_core = git_show("_shared/prewalk_core.py")
    current_core = (ROOT / "_shared" / "prewalk_core.py").read_text(encoding="utf-8")
    old_codex_hooks = git_show("codex/hooks.json")
    current_codex_hooks = (ROOT / "codex" / "hooks.json").read_text(encoding="utf-8")
    old_claude_hooks = git_show("claude-code/hooks/hooks.json")
    current_claude_hooks = (ROOT / "claude-code" / "hooks" / "hooks.json").read_text(encoding="utf-8")
    old_codex_presets = git_show("codex/presets.example.toml")
    current_codex_presets = (ROOT / "codex" / "presets.example.toml").read_text(encoding="utf-8")

    gates = {
        "planner_replacement": "planner =" in old_codex_presets and "planner =" not in current_codex_presets,
        "volatile_checkpoint": "V4_SCHEMA_VERSION" not in old_core and "V4_SCHEMA_VERSION = 4" in current_core,
        "pause_sentinel": "capture_v4_checkpoint" not in old_core and "def capture_v4_checkpoint" in current_core,
        "synthetic_codex_route": "SubagentStop" not in old_codex_hooks and "SubagentStop" in current_codex_hooks,
        "ambiguous_claude_completion": "SubagentStart" not in old_claude_hooks and "SubagentStart" in current_claude_hooks,
        "unsafe_recovery": "reconcile_v4_route" not in old_core and "def reconcile_v4_route" in current_core,
    }
    expected = set(CONTRACT["v030_regressions"]["defects"])
    if set(gates) != expected:
        raise RuntimeError(f"regression inventory mismatch: {sorted(set(gates) ^ expected)}")
    missed = [name for name, detected in gates.items() if not detected]
    if missed:
        print(f"v0.3.0 regression gates missed: {', '.join(missed)}", file=sys.stderr)
        return 1
    print(f"v0.3.0 regression gates detected all {len(gates)} known workflow defects")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
