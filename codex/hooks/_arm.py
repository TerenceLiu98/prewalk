#!/usr/bin/env python3
"""Codex prewalk arming / status / disarm helper, called by the $prewalk skill.

Usage:
  _arm.py arm    <session_id> [--preset NAME] [--no-pause] [task ...]
  _arm.py status <session_id>
  _arm.py disarm <session_id>

Reads presets from $CODEX_HOME/prewalk-presets.toml. Writes per-session state to
$CODEX_HOME/prewalk-state.json (shared with the pause/edit hooks). Prints the
frontier instructions + the model pair for the skill to surface.
"""

from __future__ import annotations

import sys
import shlex

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
        if tok == "--no-pause":
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
              "Pass it explicitly: _arm.py arm <session_id> ...",
              file=sys.stderr)
        return 1
    try:
        preset_name, auto_swap = _parse_args(rest)
    except (ValueError, shlex.Error) as exc:
        print(f"prewalk: invalid arm arguments: {exc}", file=sys.stderr)
        return 2
    presets_path = _common.presets_file()
    presets = core.load_presets_toml(presets_path)
    if not presets:
        print(
            "prewalk: no presets found at " + presets_path + ". Copy "
            "codex/presets.example.toml there first. Falling back to built-in defaults.",
            file=sys.stderr,
        )
        preset = core.Preset("default", "gpt-5.6-sol", "gpt-5.6-luna", "built-in fallback",
                             core.DEFAULT_MAX_TODOS)
    else:
        name = preset_name or core.default_preset_toml(presets_path)
        preset = presets.get(name) or next(iter(presets.values()))
        preset_name = preset.name

    core.start_run(_common.store_file(), session_id, preset, auto_swap=auto_swap)
    print(f"prewalk ARMED  [{preset.name}]  auto_swap={auto_swap}")
    print(f"  planner : {preset.planner_model}")
    print(f"  executor: {preset.executor_model}")
    print()
    print("Switch this session to the planner now by running: /model " + preset.planner_model)
    print("Then follow the frontier protocol from the $prewalk skill.")
    return 0


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
        print(core.describe(store, session_id))
        return 0
    if sub == "disarm":
        print(core.disarm(store, session_id))
        return 0
    print("unknown subcommand: " + sub, file=sys.stderr)
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
