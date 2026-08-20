#!/usr/bin/env python3
"""Claude Code prewalk arming / status / disarm helper, called by the /prewalk skill.

Usage:
  _arm.py arm    <session_id> [--preset NAME] [--fast] [task ...]
  _arm.py status <session_id>
  _arm.py disarm <session_id>
  _arm.py doctor <session_id>

Reads presets from ~/.claude/prewalk-presets.json. Writes per-session state to
~/.claude/prewalk-state.json (shared with the pause/edit hooks). Prints the
frontier instructions + the model pair for the skill to surface.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
import shlex
import re
import subprocess
import sys

import _bootstrap  # noqa: F401  (locates prewalk_core.py)
import prewalk_core as core  # noqa: E402

import _common  # type: ignore[import-not-found]  # noqa: E402


def _parse_args(rest: list[str]) -> tuple[str | None, bool]:
    """Parse leading arm options without interpreting freeform task text.

    Skills pass ``$ARGUMENTS`` as one quoted value, while direct callers may
    pass separate argv tokens. In either form, the first non-option starts the
    task and stops option parsing.
    """
    tokens = shlex.split(rest[0]) if len(rest) == 1 else rest
    auto_swap = False
    preset: str | None = None
    index = 0
    while index < len(tokens):
        tok = tokens[index]
        if tok == "--":
            break
        if tok in ("--no-pause", "--fast"):
            auto_swap = True
        elif tok == "--preset":
            if index + 1 >= len(tokens):
                raise ValueError("--preset requires a name")
            index += 1
            preset = tokens[index]
        elif tok.startswith("--preset="):
            preset = tok.partition("=")[2]
            if not preset:
                raise ValueError("--preset requires a name")
        else:
            break
        index += 1
    return preset, auto_swap


def cmd_arm(session_id: str, rest: list[str]) -> int:
    session_id = _common.resolve_session_id(session_id)
    if not session_id:
        print("prewalk: cannot arm — could not determine the session id. "
              "Ensure the SessionStart hook (export_session_id.py) is registered, "
              "or pass the id explicitly: _arm.py arm <session_id> ...",
              file=sys.stderr)
        return 1
    try:
        preset_name, auto_swap = _parse_args(rest)
    except (ValueError, shlex.Error) as exc:
        print(f"prewalk: invalid arm arguments: {exc}", file=sys.stderr)
        return 2
    presets_path = _common.presets_file()
    presets = core.load_presets_json(presets_path)
    if not presets:
        print(
            "prewalk: no presets found at " + presets_path + ". Copy "
            "claude-code/presets.example.json there first. Falling back to built-in defaults.",
            file=sys.stderr,
        )
        preset = core.Preset(
            name="default",
            executor_model="haiku",
            description="built-in fallback",
            max_todos=core.DEFAULT_MAX_TODOS,
        )
    else:
        name = preset_name or core.default_preset_json(presets_path)
        preset = presets.get(name) or next(iter(presets.values()))
        preset_name = preset.name

    report = core.evaluate_capabilities(preset, "claude", environment=dict(os.environ))
    if not report.routing_allowed:
        print("prewalk: cannot arm because required executor routing is not provable.", file=sys.stderr)
        print(core.format_capability_report(report), file=sys.stderr)
        return 1
    core.start_v4_run(
        _common.store_file(), session_id, os.getcwd(), "claude", preset, fast_mode=auto_swap
    )
    print(f"prewalk ARMED  [{preset.name}]  auto_swap={auto_swap}")
    print("  planner : active root session (Prewalk does not change it)")
    print(f"  handoff : {preset.handoff_mode} (model routing required={preset.require_model_routing})")
    print(core.format_capability_report(report))
    print()
    print("Continue in this active session and follow /prewalk:prewalk.")
    return 0


MIN_CLAUDE_VERSION = (2, 1, 145)


def _claude_version() -> tuple[tuple[int, int, int] | None, str]:
    try:
        result = subprocess.run(
            ["claude", "--version"], text=True, capture_output=True, timeout=5, check=False
        )
    except (OSError, subprocess.SubprocessError):
        return None, "not found"
    detail = (result.stdout or result.stderr).strip().splitlines()[-1]
    match = re.search(r"(\d+)\.(\d+)\.(\d+)", detail)
    return (tuple(map(int, match.groups())) if match else None), detail


def cmd_doctor(given_session_id: str) -> int:
    failures = 0

    def check(ok: bool, label: str, detail: str = "") -> None:
        nonlocal failures
        print(f"{'PASS' if ok else 'FAIL'}  {label}" + (f": {detail}" if detail else ""))
        failures += 0 if ok else 1

    check(sys.version_info >= (3, 10), "Python", sys.version.split()[0])
    check(core.VERSION == "0.3.1", "shared core", core.VERSION)
    version, version_text = _claude_version()
    check(
        version is not None and version >= MIN_CLAUDE_VERSION,
        "Claude Code >= 2.1.145",
        version_text,
    )
    resolved = _common.resolve_session_id(given_session_id)
    check(
        bool(resolved),
        "root session identity",
        "SessionStart identity available" if resolved else "missing; restart after plugin install",
    )
    presets_path = Path(_common.presets_file())
    presets = core.load_presets_json(presets_path)
    if presets_path.is_file():
        check(bool(presets), "preset parse", f"{len(presets)} preset(s) in {presets_path}")
    else:
        print(f"WARN  preset file: {presets_path} is absent; built-in defaults will be used")
    for preset_name, configured in presets.items():
        check(
            bool(configured.executor_model.strip()),
            f"model catalog/config [{preset_name}]",
            configured.executor_model or "missing executor model",
        )
        for warning in configured.deprecation_warnings:
            print(f"WARN  config deprecation [{preset_name}]: {warning}")
    manifest = Path(__file__).resolve().with_name("hooks.json")
    try:
        hooks = json.loads(manifest.read_text(encoding="utf-8"))["hooks"]
        required_events = {
            "PreToolUse", "PostToolUse", "PostToolUseFailure", "PermissionDenied",
            "SubagentStart", "SubagentStop", "Stop", "SessionStart",
        }
        lifecycle_matcher = "^(prewalk:)?prewalk-executor$"
        manifest_ok = required_events.issubset(hooks) and all(
            [group.get("matcher") for group in hooks[event]] == [lifecycle_matcher]
            for event in ("SubagentStart", "SubagentStop")
        )
    except (OSError, json.JSONDecodeError, KeyError, TypeError):
        manifest_ok = False
    check(manifest_ok, "plugin hooks", str(manifest))
    store_parent = Path(_common.store_file()).parent
    check(store_parent.is_dir() and os.access(store_parent, os.W_OK), "state directory", str(store_parent))
    preset = (
        presets.get(core.default_preset_json(presets_path)) or next(iter(presets.values()))
        if presets else core.Preset("default", "haiku")
    )
    report = core.evaluate_capabilities(preset, "claude", environment=dict(os.environ))
    print(core.format_capability_report(report))
    check(report.routing_allowed, "live executor routing", report.model_proven)
    print("WARN  model catalog API: Claude exposes no non-billable catalog; availability is launch-time")
    print("PASS  model availability policy: configured IDs are validated by Claude at Agent launch")
    return 1 if failures else 0


def main() -> int:
    if len(sys.argv) < 3:
        print(__doc__, file=sys.stderr)
        return 2
    sub, session_id = sys.argv[1], sys.argv[2]
    rest = sys.argv[3:]
    store = _common.store_file()
    if sub == "arm":
        return cmd_arm(session_id, rest)
    if sub == "status":
        workspace_id = core.workspace_identity(os.getcwd())
        core.detect_v4_stale(store, session_id, workspace_id=workspace_id)
        loaded = core.load_v4_state(
            store, session_id, workspace_id=workspace_id
        )
        print(_common.claude_commands(
            core.format_v4_status(loaded, host="claude")
        ))
        return 0
    if sub == "disarm":
        print(core.disarm(store, session_id))
        return 0
    if sub == "doctor":
        return cmd_doctor(session_id)
    print("unknown subcommand: " + sub, file=sys.stderr)
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
