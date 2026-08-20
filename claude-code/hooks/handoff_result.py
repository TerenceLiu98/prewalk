#!/usr/bin/env python3
"""Track launch success/failure for the exact routed Claude Agent tool call."""

from __future__ import annotations

import _bootstrap  # noqa: F401
import _common  # type: ignore[import-not-found]
import prewalk_core as core  # noqa: E402


def _response_text(value) -> str:
    if isinstance(value, str):
        return value
    if isinstance(value, list):
        return "\n".join(_response_text(item) for item in value)
    if not isinstance(value, dict):
        return ""
    parts: list[str] = []
    for key, item in value.items():
        if key in ("text", "content", "result", "output", "data", "message", "error"):
            parts.append(_response_text(item))
    return "\n".join(part for part in parts if part)


def main() -> int:
    payload = _common.read_input()
    sid = _common.session_id(payload)
    store = _common.store_file()
    state = core.load_state(store, sid)
    if state is None or state.phase not in (core.HANDOFF_REQUESTED, core.EXECUTOR):
        return 0

    tool_use_id = str(payload.get("tool_use_id") or payload.get("toolUseId") or "").strip()
    if not tool_use_id or tool_use_id != state.handoff_tool_use_id:
        return 0

    event = str(payload.get("hook_event_name") or payload.get("hookEventName") or "PostToolUse")
    failed_event = event in ("PostToolUseFailure", "PermissionDenied")
    response = payload.get("tool_response", payload.get("toolResponse"))
    result = _response_text(response)
    if failed_event:
        error = str(payload.get("error") or payload.get("reason") or result).strip()
        reason = error.splitlines()[0] if error else "executor Task failed or was rejected"
        action = core.on_handoff_failed(store, sid, reason)
        _common.emit(action, event=event)
        return 0

    action = core.on_handoff_launch_ack(store, sid, tool_use_id)
    if action is not None:
        _common.emit(action, event=event)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
