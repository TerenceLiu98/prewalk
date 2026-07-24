#!/usr/bin/env python3
"""Claude Code prewalk hook (v0.2) — PostToolUse on TodoWrite.

Tracks the todo list across the run: counts remaining items, notes the ⏸️ PAUSE
checkpoint, and (in --no-pause auto mode) nudges the frontier to hand off by
spawning the executor once the first edit has landed. In manual mode it just
reports progress so the user knows when to /pw-go.
"""

from __future__ import annotations

import sys

import _bootstrap  # noqa: F401  (locates prewalk_core.py)
import _common  # type: ignore[import-not-found]
import prewalk_core as core  # noqa: E402


def main() -> int:
    payload = _common.read_input()
    sid = _common.session_id(payload)
    store = _common.store_file()
    state = core.load_state(store, sid)
    if state is None:
        return 0

    todos = _common.normalize_todos(payload)
    if not todos:
        return 0

    pause_present = any(t.is_pause for t in todos)
    if pause_present:
        state.pause_seen = True
    if state.phase == core.FRONTIER:
        state.frontier_todos_ever_seen = True
    state.todos_remaining = core.count_remaining(todos)
    core.save_state(store, state)

    # Executor phase: detect completion.
    if state.phase == core.EXECUTOR:
        if state.todos_remaining == 0:
            core.clear_state(store, sid)
            _common.emit(core.HookAction(
                system_message="prewalk: all todos completed ✅",
            ), event="PostToolUse")
        return 0

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
