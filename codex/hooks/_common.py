#!/usr/bin/env python3
"""Shared I/O helpers for the Codex prewalk hooks.

All entry scripts (pause_detect.py on Stop, edit_tracker.py on PostToolUse,
todo_tracker.py on PostToolUse) import from here. This module is a thin shim over
_shared/prewalk_core.py: it resolves the store/preset files, normalizes the host's
todo shape, and renders the core HookAction into Codex's hook output contract.

Codex cannot rewrite the next request from a hook (no updatedInput), so the v0.2
handoff uses the native spawn_agent tool: the /pw-go skill prints guidance
instructing the model to pass the executor model explicitly and start a fresh
context with the handoff summary as instruction. The bundled agent TOML is
policy/reference only; Codex does not resolve it as a named spawn target.
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


def _event_part(payload: dict, snake_name: str, camel_name: str):
    if snake_name in payload:
        return payload[snake_name]
    return payload.get(camel_name)


def _todo_items(holder) -> list[dict]:
    """Find a todo list through the small set of wrappers used by hook tools."""
    if isinstance(holder, str):
        try:
            holder = json.loads(holder)
        except json.JSONDecodeError:
            return []
    if isinstance(holder, list):
        return [item for item in holder if _looks_like_todo(item)]
    if not isinstance(holder, dict):
        return []
    for key in ("todos", "tasks", "plan", "items", "steps"):
        if isinstance(holder.get(key), list):
            return [item for item in holder[key] if _looks_like_todo(item)]
    for key in ("result", "output", "data", "structured_content", "structuredContent"):
        items = _todo_items(holder.get(key))
        if items:
            return items
    return []


def _looks_like_todo(item) -> bool:
    if not isinstance(item, dict):
        return False
    has_content = any(
        key in item for key in ("content", "text", "step", "subject", "description", "title")
    )
    has_identity_or_status = any(key in item for key in ("id", "uuid", "status", "state"))
    return has_content and has_identity_or_status


def _item_to_todo(item: dict) -> core.Todo:
    content = str(
        item.get("content")
        or item.get("text")
        or item.get("step")
        or item.get("subject")
        or item.get("description")
        or item.get("title")
        or ""
    )
    status = str(item.get("status") or item.get("state") or "").lower()
    return core.Todo(
        id=str(item.get("id") or item.get("uuid") or content[:40]),
        content=content,
        status=status,
    )


def normalize_todos(payload: dict) -> list[core.Todo]:
    """Read todos from tool_input (PreToolUse) or tool_response (PostToolUse/Stop).

    Codex's plan/todo tool carries items as dicts. Field names vary by tool
    (`update_plan` vs `todo`); we accept content/status under common keys."""
    for holder in (
        _event_part(payload, "tool_input", "toolInput"),
        _event_part(payload, "tool_response", "toolResponse"),
    ):
        items = _todo_items(holder)
        if items:
            return [_item_to_todo(item) for item in items]
    return []


def normalize_edit_success(payload: dict) -> bool:
    """Return whether a PostToolUse edit payload represents a successful edit."""
    response = _event_part(payload, "tool_response", "toolResponse")
    if response is None or response is False:
        return False
    return not _has_explicit_failure(response)


def _has_explicit_failure(value) -> bool:
    if isinstance(value, list):
        return any(_has_explicit_failure(item) for item in value)
    if not isinstance(value, dict):
        return False
    if value.get("is_error") is True or value.get("isError") is True:
        return True
    if any(value.get(key) is False for key in ("ok", "success", "executed")):
        return True
    if value.get("error"):
        return True
    if str(value.get("status", "")).lower() in ("error", "failed", "failure"):
        return True
    return any(
        _has_explicit_failure(value.get(key))
        for key in ("result", "output", "data", "structured_content", "structuredContent")
    )


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
