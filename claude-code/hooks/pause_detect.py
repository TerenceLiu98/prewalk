#!/usr/bin/env python3
"""Claude root Stop hook: validate and persist the exact v4 checkpoint packet."""

from __future__ import annotations

import _bootstrap  # noqa: F401
import _common  # type: ignore[import-not-found]
import prewalk_core as core  # noqa: E402


def main() -> int:
    payload = _common.read_input()
    sid = _common.session_id(payload)
    if not sid:
        return 0
    store = _common.store_file()
    packet = str(
        payload.get("last_assistant_message")
        or payload.get("lastAssistantMessage")
        or ""
    )
    event_id = str(payload.get("event_id") or payload.get("eventId") or "")
    result = core.capture_v4_checkpoint(
        store,
        sid,
        packet=packet,
        todos=_common.normalize_todos(payload) or None,
        event_id=event_id,
    )
    if result.message:
        _common.emit(core.HookAction(system_message=result.message), event="Stop")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
