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
    reason = str(payload.get("reason") or payload.get("stop_reason") or payload.get("stopReason") or "")
    interrupted = core.interrupt_v4_executor(
        store,
        sid,
        reason=reason,
        event_id=str(payload.get("event_id") or payload.get("eventId") or ""),
    )
    if interrupted.handled:
        _common.emit(core.HookAction(system_message=interrupted.message), event="Stop")
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
        if (
            result.status == "checkpoint_ready"
            and result.state is not None
            and result.state.fast_mode
        ):
            core.apply_v4_transition(
                store,
                sid,
                expected_phases=[core.V4_CHECKPOINT_READY],
                target_phase=core.V4_CHECKPOINT_READY,
                event_id=f"codex-fast-continuation:{event_id or result.state.revision}",
                updates={"fast_mode": False},
            )
            _common.emit(core.HookAction(
                proceed=False,
                block_reason=(
                    "Prewalk fast checkpoint is durable. Inspect the live spawn_agent schema, "
                    "then run $prewalk:pw-go and execute its exact token-bound route now."
                ),
                system_message=result.message,
            ), event="Stop")
        else:
            _common.emit(core.HookAction(system_message=result.message), event="Stop")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
