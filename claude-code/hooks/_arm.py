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
    core.start_run(_common.store_file(), session_id, preset, auto_swap=auto_swap)
    print(f"prewalk ARMED  [{preset.name}]  auto_swap={auto_swap}")
    print("  planner : active root session (Prewalk does not change it)")
    print(f"  handoff : {preset.handoff_mode} (model routing required={preset.require_model_routing})")
    print(core.format_capability_report(report))
    print()
    print("Continue in this active session and follow /prewalk:prewalk.")
    return 0


def cmd_doctor() -> int:
    failures = 0

    def check(ok: bool, label: str, detail: str = "") -> None:
        nonlocal failures
        print(f"{'PASS' if ok else 'FAIL'}  {label}" + (f": {detail}" if detail else ""))
        failures += 0 if ok else 1

    check(sys.version_info >= (3, 10), "Python", sys.version.split()[0])
    check(core.VERSION == "0.3.1", "shared core", core.VERSION)
    presets_path = Path(_common.presets_file())
    presets = core.load_presets_json(presets_path)
    if presets_path.is_file():
        check(bool(presets), "preset parse", f"{len(presets)} preset(s) in {presets_path}")
    else:
        print(f"WARN  preset file: {presets_path} is absent; built-in defaults will be used")
    manifest = Path(__file__).resolve().with_name("hooks.json")
    try:
        json.loads(manifest.read_text(encoding="utf-8"))
        manifest_ok = True
    except (OSError, json.JSONDecodeError):
        manifest_ok = False
    check(manifest_ok, "hook manifest", str(manifest))
    store_parent = Path(_common.store_file()).parent
    check(store_parent.is_dir() and os.access(store_parent, os.W_OK), "state directory", str(store_parent))
    preset = (
        presets.get(core.default_preset_json(presets_path)) or next(iter(presets.values()))
        if presets else core.Preset("default", "haiku")
    )
    report = core.evaluate_capabilities(preset, "claude", environment=dict(os.environ))
    print(core.format_capability_report(report))
    check(report.routing_allowed, "executor model routing", report.model_proven)
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
        print(_common.claude_commands(core.describe(store, session_id)))
        state = core.load_state(store, session_id)
        if state is not None:
            presets = core.load_presets_json(_common.presets_file())
            preset = presets.get(state.preset) or core.Preset(
                state.preset, state.executor_model, executor_effort=state.executor_thinking
            )
            print(core.format_capability_report(
                core.evaluate_capabilities(preset, "claude", environment=dict(os.environ))
            ))
        return 0
    if sub == "disarm":
        print(_common.claude_commands(core.disarm(store, session_id)))
        return 0
    if sub == "doctor":
        return cmd_doctor()
    print("unknown subcommand: " + sub, file=sys.stderr)
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
