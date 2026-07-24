#!/usr/bin/env python3
"""Shared I/O helpers for the Codex prewalk hooks.

Both entry scripts (pause_detect.py on Stop, edit_gate.py on PreToolUse) import
from here. This module is a thin shim over _shared/prewalk_core.py: it resolves
the store/preset files, normalizes the host's todo shape, and renders the core
HookAction into Codex's hook output contract.

Codex cannot switch the model from a hook/MCP tool — there is no such API. So at
the paused checkpoint we surface guidance whose `reason` instructs the model to
run `/model <executor>`; Codex's TUI parses queued slash commands. (Alternatively
drive the app-server `turn/start { model }`.) See README "Style A".
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path


import _bootstrap  # noqa: F401  (locates prewalk_core.py)
import prewalk_core as core  # noqa: E402


def codez_home() -> str:
    return os.environ.get("CODEX_HOME") or os.path.expanduser("~/.codex")


def store_file() -> str:
    return os.path.join(codez_home(), "prewalk-state.json")


def presets_file() -> str:
    return os.path.join(codez_home(), "prewalk-presets.toml")


def read_input() -> dict:
    raw = sys.stdin.read()
    if not raw.strip():
        return {}
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        return {}


def session_id(payload: dict) -> str:
    return str(payload.get("session_id") or payload.get("sessionId") or "")


def resolve_session_id(given: str) -> str:
    """Return a non-empty session id, deriving one if the caller passed none.

    Codex does not reliably expose the session id to a skill's bash subprocess,
    so $CODEX_SESSION_ID may be empty. As a safety net, derive the id from the
    most-recently-modified rollout file under $CODEX_HOME/sessions (the UUID in
    its filename is the session id). This can race under concurrent sessions in
    the same CODEX_HOME; prefer having the skill pass the id explicitly."""
    given = (given or "").strip()
    if given:
        return given
    import glob
    root = os.path.join(codez_home(), "sessions")
    try:
        files = sorted(glob.glob(os.path.join(root, "**", "rollout-*.jsonl"), recursive=True),
                       key=os.path.getmtime, reverse=True)
    except OSError:
        files = []
    if files:
        name = os.path.basename(files[0])  # rollout-<ts>-<uuid>.jsonl
        # uuid is the last dash-separated segment before .jsonl
        stem = name[:-6] if name.endswith(".jsonl") else name
        parts = stem.split("-")
        # timestamp has fixed dashes; uuid is the trailing 5 groups — take last 5
        return "-".join(parts[-5:]) if len(parts) >= 5 else stem
    return ""


def normalize_todos(payload: dict) -> list[core.Todo]:
    """Read todos from tool_input (PreToolUse) or tool_response (PostToolUse/Stop).

    Codex's plan/todo tool carries items as dicts. Field names vary by tool
    (`update_plan` vs `todo`); we accept content/status under common keys."""
    src = None
    ti = payload.get("tool_input") or {}
    if isinstance(ti, dict):
        for key in ("todos", "plan", "items", "steps"):
            if isinstance(ti.get(key), list):
                src = ti[key]
                break
    if src is None:
        tr = payload.get("tool_response") or {}
        if isinstance(tr, dict):
            for key in ("todos", "plan", "items", "steps"):
                if isinstance(tr.get(key), list):
                    src = tr[key]
                    break
        elif isinstance(tr, list):
            src = tr
    if not src:
        return []
    out = []
    for item in src:
        if not isinstance(item, dict):
            continue
        content = str(item.get("content") or item.get("text") or item.get("step") or "")
        status = str(item.get("status") or item.get("state") or "")
        out.append(core.Todo(id=str(item.get("id") or content[:40]), content=content, status=status))
    return out


def emit(action: core.HookAction | None, *, event: str, deny_as_permission: bool = False) -> None:
    """Render a core HookAction to Codex stdout JSON (or print nothing).

    Codex's Stop/UserPromptSubmit accept top-level decision:block + reason, which
    creates a new continuation prompt. PreToolUse carries its deny decision inside
    hookSpecificOutput.permissionDecision. additionalContext is supported across
    events."""
    if action is None:
        return
    out: dict = {}
    if action.system_message:
        out["systemMessage"] = action.system_message
    if not action.proceed:
        if deny_as_permission:
            out["hookSpecificOutput"] = {
                "hookEventName": event or "PreToolUse",
                "permissionDecision": "deny",
                "permissionDecisionReason": action.block_reason,
            }
        else:
            out["decision"] = "block"
            out["reason"] = action.block_reason
    elif action.additional_context:
        out["hookSpecificOutput"] = {
            "hookEventName": event or "Stop",
            "additionalContext": action.additional_context,
        }
    if out:
        sys.stdout.write(json.dumps(out, ensure_ascii=False))
