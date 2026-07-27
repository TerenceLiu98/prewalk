#!/usr/bin/env python3
"""Claude Code prewalk hook (v0.3) — PostToolUse on todo/task tools.

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

    # The shared core validates the full checkpoint and owns phase changes.
    if state.phase in (core.FRONTIER, core.READY):
        action = core.on_todos_changed(store, sid, todos)
        _common.emit(action, event="PostToolUse")
        state = core.load_state(store, sid)
        if state is None:
            return 0

    # Executor phase: detect completion.
    if state.phase == core.EXECUTOR:
        if state.todos_remaining == 0:
            action = core.on_executor_result(store, sid, complete=True)
            _common.emit(action, event="PostToolUse")
        return 0

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
