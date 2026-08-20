#!/usr/bin/env python3
"""Codex root Stop hook: validate and persist the exact v4 checkpoint packet.

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
    if not sid:
        return 0
    packet = str(
        payload.get("last_assistant_message")
        or payload.get("lastAssistantMessage")
        or ""
    )
    event_id = str(payload.get("event_id") or payload.get("eventId") or "")
    result = core.capture_v4_checkpoint(
        store, sid, packet=packet, todos=todos or None, event_id=event_id
    )
    if result.message:
        _common.emit(core.HookAction(system_message=result.message), event="Stop")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
