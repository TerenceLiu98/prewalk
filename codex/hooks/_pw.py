#!/usr/bin/env python3
"""Codex prewalk handoff/revision helper, called by /pw-go and /pw-revise.

Usage:
  _pw.py go      <session_id>
  _pw.py revise  <session_id> [revision text...]
  _pw.py resume  <session_id>
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
        fields: set[str] = set()
        for argument in sys.argv[3:]:
            if argument.startswith("--schema-fields="):
                fields.update(
                    item.strip() for item in argument.partition("=")[2].split(",") if item.strip()
                )
        result = core.request_codex_handoff(store, session_id, schema_fields=fields)
        if result.state is not None and result.status == "handoff_requested":
            state = result.state
            print(f"PREWALK_TASK_NAME: {state.route_task_name}")
            print(f"PREWALK_EXECUTOR_MODEL: {state.executor_model}")
            if state.effort_routing_proven:
                print(f"PREWALK_EXECUTOR_EFFORT: {state.executor_effort}")
            print("PREWALK_FORK_TURNS: none")
            print("PREWALK_MESSAGE_BEGIN")
            print(result.message)
            print("PREWALK_MESSAGE_END")
        else:
            print(result.message or "There is no active prewalk checkpoint in this session.")
        return 0

    if sub == "revise":
        revision = " ".join(sys.argv[3:]).strip()
        result = core.revise_v4_checkpoint(store, session_id, revision)
        print(result.message or "There is no active prewalk checkpoint to revise.")
        return 0

    if sub == "resume":
        result = core.resume_codex_manual(store, session_id)
        print(result.message)
        return 0

    if sub in ("complete", "incomplete"):
        result = core.finish_codex_manual(
            store,
            session_id,
            complete=sub == "complete",
            detail=" ".join(sys.argv[3:]),
        )
        print(result.message)
        return 0

    print("unknown subcommand: " + sub, file=sys.stderr)
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
