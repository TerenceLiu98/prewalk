#!/usr/bin/env python3
"""Codex prewalk hook (v0.2) — PostToolUse on apply_patch.

When the frontier model lands its first successful edit, mark the run
"handoff-ready". After that point, `/pw-go` can hand off to the executor
subagent.

This replaces the old edit_gate (which blocked edits before a todo existed and
disarmed on a 2nd violation — that design enraged the model into bypassing
prewalk). The new design never blocks edits: the frontier is free to explore
and edit; we only observe and arm the handoff.

Note: Codex's PreToolUse/PostToolUse only intercept "simple" shell calls and
file-edit tools (apply_patch), not WebSearch or complex shell pipelines. The
edit observation therefore keys off apply_patch (the file-edit surface), which
is what matters for prewalk.
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
        # apply_patch returns {ok, file, ...} or an error; treat absence of error as ok.
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
