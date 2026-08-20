#!/usr/bin/env python3
"""Bind and finish only the routed Prewalk executor subagent."""

from __future__ import annotations

import _bootstrap  # noqa: F401
import _common  # type: ignore[import-not-found]
import prewalk_core as core  # noqa: E402


EXECUTOR_AGENT = "prewalk:prewalk-executor"


def main() -> int:
    payload = _common.read_input()
    if str(payload.get("agent_type") or payload.get("agentType") or "") != EXECUTOR_AGENT:
        return 0

    sid = _common.session_id(payload, allow_subagent=True)
    store = _common.store_file()
    state = core.load_state(store, sid)
    if state is None:
        return 0

    event = str(payload.get("hook_event_name") or payload.get("hookEventName") or "")
    agent_id = str(payload.get("agent_id") or payload.get("agentId") or "").strip()
    if event == "SubagentStart":
        action = core.on_executor_started(store, sid, agent_id)
        if action is not None:
            _common.emit(action, event=event)
        return 0

    if event != "SubagentStop" or state.phase != core.EXECUTOR:
        return 0
    if not agent_id or agent_id != state.executor_agent_id:
        return 0

    result = str(payload.get("last_assistant_message") or payload.get("lastAssistantMessage") or "")
    lines = [line.strip() for line in result.splitlines() if line.strip()]
    if "PREWALK_COMPLETE" in lines:
        action = core.on_executor_result(store, sid, complete=True)
    elif any(line.startswith("PREWALK_INCOMPLETE:") for line in lines):
        marker = next(line for line in lines if line.startswith("PREWALK_INCOMPLETE:"))
        action = core.on_executor_result(
            store, sid, complete=False, detail=marker.partition(":")[2].strip()
        )
    else:
        action = core.on_executor_result(
            store,
            sid,
            complete=False,
            detail="bound executor stopped without a PREWALK completion marker",
        )
    _common.emit(action, event=event)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
