#!/usr/bin/env python3
"""Claude Code prewalk hook — edit gate (PreToolUse).

Blocks Write/Edit/MultiEdit during the frontier phase until a valid, capped
todo list with validation checkpoints exists. Disarms after a second violation.
Reads the *current* todo list from the hook payload (TodoWrite tool_input is
not present on an edit call, so if no todos are visible the gate treats the
list as empty — which is the correct "not yet planned" state).
"""

from __future__ import annotations

import sys

import _bootstrap  # noqa: F401  (locates prewalk_core.py)
import _common  # type: ignore[import-not-found]


def main() -> int:
    payload = _common.read_input()
    sid = _common.session_id(payload)
    store = _common.store_file()
    todos = _common.normalize_todos(payload)
    action = __import__("prewalk_core").on_edit_attempt(store, sid, todos)
    _common.emit(action, event="PreToolUse", deny_as_permission=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
