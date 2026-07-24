#!/usr/bin/env python3
"""Claude Code prewalk hook — pause detection + state machine driver.

Register on:
  - Stop        : trivial/anomaly cleanup at turn end
  - PostToolUse : TodoWrite — drives frontier→paused→executor

See _common.py for the host I/O contract and ../README.md for the flow.
"""

from __future__ import annotations

import sys

import _bootstrap  # noqa: F401  (locates prewalk_core.py)
import _common  # type: ignore[import-not-found]


def main() -> int:
    payload = _common.read_input()
    event = str(payload.get("hook_event_name") or "PostToolUse")
    sid = _common.session_id(payload)
    store = _common.store_file()

    if event == "Stop":
        _common.emit(__import__("prewalk_core").on_turn_end(store, sid), event="Stop")
        return 0

    if event == "PostToolUse":
        todos = _common.normalize_todos(payload)
        if not todos:
            return 0  # not a TodoWrite call we care about
        action = __import__("prewalk_core").on_todos_changed(store, sid, todos)
        _common.emit(action, event="PostToolUse")
        return 0

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
