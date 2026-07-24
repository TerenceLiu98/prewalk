#!/usr/bin/env python3
"""Claude Code prewalk hook (v0.2) — PreToolUse on Task (the handoff engine).

This is the core of the subagent-routing design, borrowed from
tzachbon/claude-model-router-hook. Claude Code cannot switch the running
session's model from a hook, BUT a PreToolUse hook can rewrite a subagent
spawn's `tool_input` via hookSpecificOutput.updatedInput — including its
`model` and `subagent_type`. So instead of switching the model mid-session, we
hand off by spawning a fresh `prewalk-executor` subagent with the executor model
forced on.

Flow:
  - frontier (main session) explores + plans + lands edit #1
  - edit_tracker sets phase="ready" on the first successful edit
  - the frontier's next Task spawn (or the user's /pw-go) is rewritten here into
    { subagent_type: "prewalk-executor", model: <executor>, prompt: <handoff> }
  - once handed off, phase="executor"; subsequent Task spawns are left alone so
    the executor can spawn its own helpers if needed.

Never denies: emits permissionDecision "allow" + updatedInput, or exits 0.
"""

from __future__ import annotations

import json
import os
import sys

import _bootstrap  # noqa: F401  (locates prewalk_core.py)
import _common  # type: ignore[import-not-found]
import prewalk_core as core  # noqa: E402

EXECUTOR_AGENT = "prewalk-executor"
PLUGIN_PREFIX = "prewalk:"  # plugin-scoped name when installed as a plugin


def _executor_agent_name() -> str:
    """Use the plugin-scoped name if the agent file ships with this plugin;
    otherwise the bare name resolves against ~/.claude/agents."""
    root = os.environ.get("CLAUDE_PLUGIN_ROOT")
    if root and os.path.exists(os.path.join(root, "agents", EXECUTOR_AGENT + ".md")):
        return PLUGIN_PREFIX + EXECUTOR_AGENT
    return EXECUTOR_AGENT


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

    tool_input = payload.get("tool_input")
    if not isinstance(tool_input, dict):
        return 0

    # Only hand off once; after that the executor owns the work.
    if state.phase != core.READY or state.handoff_done:
        return 0

    executor_model = state.executor_model
    if not executor_model:
        return 0  # nothing to force — leave the spawn alone

    # Rewrite the spawn into the executor subagent with the model forced on.
    updated = dict(tool_input)
    updated["subagent_type"] = _executor_agent_name()
    updated["model"] = executor_model
    # Prepend the handoff protocol to whatever prompt the caller supplied.
    existing_prompt = str(updated.get("prompt") or updated.get("description") or "")
    updated["prompt"] = (_handoff_prompt(state) + "\n\n--- Task ---\n" + existing_prompt).strip()
    updated.pop("description", None)

    state.phase = core.EXECUTOR
    state.handoff_done = True
    core.save_state(store, state)

    out = {
        "hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "permissionDecision": "allow",
            "updatedInput": updated,
        },
        "systemMessage": (
            f"prewalk: handoff → spawned {EXECUTOR_AGENT} on {executor_model} "
            f"(remaining todos: {state.todos_remaining})"
        ),
    }
    sys.stdout.write(json.dumps(out, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
