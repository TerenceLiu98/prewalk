#!/usr/bin/env python3
"""Claude Code prewalk hook — route a requested Task to the executor.

This is the core of the subagent-routing design, borrowed from
tzachbon/claude-model-router-hook. Claude Code cannot switch the running
session's model from a hook, BUT a PreToolUse hook can rewrite a subagent
spawn's `tool_input` via hookSpecificOutput.updatedInput — including its
`model` and `subagent_type`. So instead of switching the model mid-session, we
hand off by spawning a fresh `prewalk-executor` subagent with the executor model
forced on.

Flow:
  - frontier (main session) explores + plans + lands edit #1
  - /pw-go sets phase="handoff_requested"
  - the frontier's token-bearing Task spawn is rewritten here into
    { subagent_type: "prewalk-executor", model: <executor>, prompt: <handoff> }
  - PostToolUse acknowledges launch; SubagentStart/Stop own executor identity and result.

Never denies: emits permissionDecision "allow" + updatedInput, or exits 0.
"""

from __future__ import annotations

import json
import sys

import _bootstrap  # noqa: F401  (locates prewalk_core.py)
import _common  # type: ignore[import-not-found]
import prewalk_core as core  # noqa: E402

EXECUTOR_AGENT = "prewalk:prewalk-executor"
TOKEN_PREFIX = "PREWALK_HANDOFF_TOKEN: "


def _deny_route(store: str, sid: str, reason: str) -> int:
    action = core.on_handoff_failed(store, sid, reason)
    out = {
        "hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "permissionDecision": "deny",
            "permissionDecisionReason": reason,
        },
        "systemMessage": _common.claude_commands(action.system_message),
    }
    sys.stdout.write(json.dumps(out, ensure_ascii=False))
    return 0


def _handoff_prompt(state) -> str:
    return (
        "PREWALK HANDOFF. The frontier model already explored the codebase, wrote "
        "the todo list, and completed + verified task #1 — its summary is in the "
        "delegation prompt below. Trust it; do NOT redo the exploration.\n\n"
        "1. Work the remaining todos strictly in order, one at a time. Never "
        "batch-complete.\n"
        "2. Mark an item in_progress before working it; run its verification and "
        "mark completed only after it passes.\n"
        "3. Imitate the pattern, style, and verification cadence of task #1.\n"
        "4. Do not re-read files already summarized unless an edit needs fresh "
        "context.\n"
        "5. Done = every todo completed or explicitly cancelled with a reason. "
        "Report anything you could not finish.\n"
        f"\n[planner was {state.original_model}; you are the {state.executor_model} executor]"
    )


def main() -> int:
    payload = _common.read_input()
    sid = _common.session_id(payload)
    store = _common.store_file()
    state = core.load_state(store, sid)
    if state is None:
        return 0

    # Plugin hooks also run inside subagents. Only the root session may consume
    # the one-time handoff token and bind an executor spawn.
    if payload.get("agent_id") or payload.get("agentId"):
        return 0

    tool_input = payload.get("tool_input")
    if not isinstance(tool_input, dict):
        return 0

    # /prewalk:pw-go must explicitly request the handoff. A token prevents an
    # unrelated or concurrent Agent call from consuming the pending route.
    if state.phase != core.HANDOFF_REQUESTED or state.handoff_done or state.handoff_routed:
        return 0

    existing_prompt = str(tool_input.get("prompt") or tool_input.get("description") or "")
    token_line = TOKEN_PREFIX + state.handoff_token
    if not state.handoff_token or token_line not in existing_prompt.splitlines():
        return 0

    executor_model = state.executor_model
    if not executor_model:
        return _deny_route(store, sid, "Prewalk cannot route an Agent call without an executor model.")

    tool_use_id = str(payload.get("tool_use_id") or payload.get("toolUseId") or "").strip()
    if not tool_use_id:
        return _deny_route(
            store, sid, "Prewalk cannot safely route an Agent call without tool_use_id."
        )

    # Rewrite the spawn into the exact plugin-scoped executor with the model forced on.
    updated = dict(tool_input)
    updated["subagent_type"] = EXECUTOR_AGENT
    updated["model"] = executor_model
    # Prepend the handoff protocol to whatever prompt the caller supplied.
    updated["prompt"] = (_handoff_prompt(state) + "\n\n--- Task ---\n" + existing_prompt).strip()
    updated.pop("description", None)

    state.handoff_routed = True
    state.handoff_token = ""
    state.handoff_tool_use_id = tool_use_id
    state.executor_agent_id = ""
    state.handoff_launch_acknowledged = False
    core.save_state(store, state)

    out = {
        "hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "permissionDecision": "allow",
            "updatedInput": updated,
        },
        "systemMessage": (
            f"prewalk: routed Agent {tool_use_id} to {EXECUTOR_AGENT} on {executor_model}; "
            "waiting for the bound subagent lifecycle"
        ),
    }
    sys.stdout.write(json.dumps(out, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
