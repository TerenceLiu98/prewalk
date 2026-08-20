#!/usr/bin/env python3
"""Codex PostToolUse hook that persists complete real-work plan snapshots.

Note: Codex's plan/todo tools use update_plan and todo with items as dicts;
the normalize_todos helper accepts content/status under keys like content/text/step
and status/state.
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
    loaded = core.load_v4_state(store, sid)
    if loaded.state is None or loaded.state.phase != core.V4_PLANNING:
        return 0

    todos = _common.normalize_todos(payload)
    if not todos or not _common.has_complete_todo_snapshot(payload):
        return 0

    result = core.record_v4_todos(store, sid, todos)
    if result.status == "invalid_todos":
        _common.emit(core.HookAction(system_message=result.message), event="PostToolUse")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
