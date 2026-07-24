#!/usr/bin/env python3
"""Codex prewalk hook — pause detection + state machine driver (Stop event).

Codex fires `Stop` at turn end. We use it both for checkpoint detection (the
frontier ends its turn at the ⏸️ PAUSE todo) and for trivial/anomaly cleanup.
Codex's todo tool calls are not always surfaced as a separate PostToolUse with
a clean todos array, so the Stop hook is the reliable place to read the current
plan. To get the current todos here we read them from the tool_response of the
last plan/todo tool call included in the hook payload (best effort); if absent,
we fall back to on_turn_end cleanup.

See _common.py for the host I/O contract and ../README.md for the flow.
"""

from __future__ import annotations

import sys

import _bootstrap  # noqa: F401  (locates prewalk_core.py — must precede the import below)
import _common  # type: ignore[import-not-found]
import prewalk_core as core  # noqa: E402


def main() -> int:
    payload = _common.read_input()
    event = str(payload.get("hook_event_name") or "Stop")
    sid = _common.session_id(payload)
    store = _common.store_file()

    if event != "Stop":
        return 0

    todos = _common.normalize_todos(payload)
    state = core.load_state(store, sid)

    # If we have todos and an active run, drive the checkpoint machine; the
    # result may transition frontier→paused (and, in auto_swap, perform the
    # switch — but Codex can't switch from a hook, so auto_swap instead returns
    # a guidance reason instructing /model).
    if todos and state is not None and state.phase in (core.FRONTIER, core.PAUSED, core.EXECUTOR):
        action = core.on_todos_changed(store, sid, todos)
        _common.emit(action, event="Stop")
        return 0

    # Otherwise: turn-end cleanup (trivial path / anomaly).
    _common.emit(core.on_turn_end(store, sid), event="Stop")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
