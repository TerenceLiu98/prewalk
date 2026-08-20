#!/usr/bin/env python3
"""Codex PostToolUse mutation observer.

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
    # V4 proves checkpoint readiness only from the root Stop snapshot and exact
    # packet. Mutation events cannot independently advance the state machine.
    core.load_v4_state(store, sid)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
