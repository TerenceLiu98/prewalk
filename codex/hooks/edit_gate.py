#!/usr/bin/env python3
"""Codex prewalk hook — edit gate (PreToolUse on apply_patch).

Blocks `apply_patch` during the frontier phase until a valid, capped todo list
with validation checkpoints exists. Disarms after a second violation. Reads the
current todo list from the hook payload's tool_input/tool_response (best
effort); an edit call normally carries no todos, so the gate treats an absent
list as empty — the correct "not yet planned" state.

Note: Codex's PreToolUse does not intercept every shell call, only "simple"
ones, and does not intercept WebSearch. The edit gate therefore keys off
`apply_patch` (the file-edit surface), which is what matters for prewalk.
"""

from __future__ import annotations

import sys

import _bootstrap  # noqa: F401  (locates prewalk_core.py — must precede the import below)
import _common  # type: ignore[import-not-found]
import prewalk_core as core  # noqa: E402


def main() -> int:
    payload = _common.read_input()
    sid = _common.session_id(payload)
    store = _common.store_file()
    todos = _common.normalize_todos(payload)
    action = core.on_edit_attempt(store, sid, todos)
    _common.emit(action, event="PreToolUse", deny_as_permission=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
