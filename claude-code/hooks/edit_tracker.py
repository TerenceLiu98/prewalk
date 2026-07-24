#!/usr/bin/env python3
"""Claude Code prewalk hook (v0.2) — PostToolUse on edits.

When the frontier model lands its first successful edit, mark the run
"handoff-ready". After that point, the handoff_router (PreToolUse on Task)
rewrites the next subagent spawn into a prewalk-executor with the executor
model forced on.

This replaces the old edit_gate (which blocked edits before a todo existed and
disarmed on a 2nd violation — that design assumed a request-rewrite middleware
that Claude Code doesn't have, and it enraged the model into bypassing prewalk).
The new design never blocks edits: the frontier is free to explore and edit;
we only observe and arm the handoff.
"""

from __future__ import annotations

import sys

import _bootstrap  # noqa: F401  (locates prewalk_core.py)
import _common  # type: ignore[import-not-found]
import prewalk_core as core  # noqa: E402


def main() -> int:
    payload = _common.read_input()
    sid = _common.session_id(payload)
    store = _common.store_file()
    state = core.load_state(store, sid)
    if state is None or state.phase != core.FRONTIER:
        return 0  # not arming, or already past frontier

    # Only count successful edits.
    tool_response = payload.get("tool_response") or {}
    ok = False
    if isinstance(tool_response, dict):
        # Write/Edit return {ok, file, ...} or an error; treat absence of error as ok.
        ok = not str(tool_response.get("error", "")).strip() or tool_response.get("ok", True) is not False
    if not ok:
        return 0

    if not state.first_edit_landed:
        state.first_edit_landed = True
        state.phase = "ready"  # frontier done its part; handoff-armed
        core.save_state(store, state)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
