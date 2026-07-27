#!/usr/bin/env python3
"""Confirm a routed Claude Task only after its PostToolUse result arrives."""

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
    if state is None or state.phase != core.HANDOFF_REQUESTED or not state.handoff_routed:
        return 0

    event = str(payload.get("hook_event_name") or payload.get("hookEventName") or "PostToolUse")
    response = payload.get("tool_response", payload.get("toolResponse"))
    result = _response_text(response)
    failed_event = event in ("PostToolUseFailure", "PermissionDenied")
    if failed_event or not _common.normalize_edit_success(payload):
        error = str(payload.get("error") or payload.get("reason") or result).strip()
        reason = error.splitlines()[0] if error else "executor Task failed or was rejected"
        action = core.on_handoff_failed(store, sid, reason)
        _common.emit(action, event=event)
        return 0

    lines = [line.strip() for line in result.splitlines() if line.strip()]
    core.on_handoff_confirm(store, sid)
    if "PREWALK_COMPLETE" in lines:
        action = core.on_executor_result(store, sid, complete=True)
    elif any(line.startswith("PREWALK_INCOMPLETE:") for line in lines):
        marker = next(line for line in lines if line.startswith("PREWALK_INCOMPLETE:"))
        detail = marker.partition(":")[2].strip()
        action = core.on_executor_result(store, sid, complete=False, detail=detail)
    else:
        action = core.on_executor_result(
            store, sid, complete=False, detail="executor returned without a PREWALK completion marker"
        )
    _common.emit(action, event=event)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
