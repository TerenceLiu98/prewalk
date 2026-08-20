#!/usr/bin/env python3
"""Codex prewalk handoff/revision helper, called by /pw-go and /pw-revise.

Usage:
  _pw.py go      <session_id>
  _pw.py revise  <session_id> [revision text...]
  _pw.py confirm <session_id>
  _pw.py fail    <session_id> [reason...]
  _pw.py complete|incomplete <session_id> [detail...]

Prints the handoff note or revision instructions (or a no-active-checkpoint
message) for the skill to surface to the model.
"""

from __future__ import annotations

import sys

import _bootstrap  # noqa: F401  (locates prewalk_core.py)
import prewalk_core as core  # noqa: E402

import _common  # type: ignore[import-not-found]  # noqa: E402


def main() -> int:
    if len(sys.argv) < 3:
        print(__doc__, file=sys.stderr)
        return 2
    sub = sys.argv[1]
    session_id = _common.resolve_session_id(sys.argv[2])
    if not session_id:
        print(
            "prewalk: cannot continue — CODEX_THREAD_ID is missing or conflicts with the supplied id. "
            "Use Codex CLI 0.146.0 or newer, or pass an explicit id on a legacy CLI.",
            file=sys.stderr,
        )
        return 1
    store = _common.store_file()

    if sub == "go":
        action = core.on_pw_go(store, session_id, host="codex")
        print(action.additional_context or action.system_message)
        if action.system_message:
            print()
            print(action.system_message)
        return 0

    if sub == "revise":
        revision = " ".join(sys.argv[3:]).strip()
        action = core.on_pw_revise(store, session_id, revision)
        print(action.additional_context or action.system_message)
        return 0

    if sub in ("confirm", "resume"):
        action = core.on_handoff_confirm(store, session_id)
        print(action.additional_context or action.system_message)
        return 0

    if sub == "fail":
        action = core.on_handoff_failed(store, session_id, " ".join(sys.argv[3:]))
        print(action.additional_context or action.system_message)
        return 0

    if sub in ("complete", "incomplete"):
        action = core.on_executor_result(
            store,
            session_id,
            complete=sub == "complete",
            detail=" ".join(sys.argv[3:]),
        )
        print(action.additional_context or action.system_message)
        return 0

    print("unknown subcommand: " + sub, file=sys.stderr)
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
