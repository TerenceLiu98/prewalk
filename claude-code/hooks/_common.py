#!/usr/bin/env python3
"""Shared I/O helpers for the Claude Code prewalk hooks.

Both entry scripts (pause_detect.py on Stop/PostToolUse, edit_gate.py on
PreToolUse) import from here. This module is a thin shim over
_shared/prewalk_core.py: it resolves the store/preset files, normalizes the
host's todo shape, and renders the core HookAction into Claude Code's hook
output contract.

Claude Code cannot switch the model from a hook — so the paused checkpoint
only surfaces guidance; the actual `/model <executor>` switch is issued by the
/pw-go skill (or the user). See README "Style A".
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path


import _bootstrap  # noqa: F401  (locates prewalk_core.py)
import prewalk_core as core  # noqa: E402


def store_file() -> str:
    home = os.environ.get("CLAUDE_CONFIG_DIR") or os.path.expanduser("~/.claude")
    return os.path.join(home, "prewalk-state.json")


def presets_file() -> str:
    home = os.environ.get("CLAUDE_CONFIG_DIR") or os.path.expanduser("~/.claude")
    return os.path.join(home, "prewalk-presets.json")


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

    Claude Code does not expose the session id to Bash-tool subprocesses
    (anthropics/claude-code#20132). The /prewalk skill passes "$CLAUDE_SESSION_ID",
    which is empty unless a SessionStart hook populated it via CLAUDE_ENV_FILE
    (see export_session_id.py). As a safety net, when the given id is empty we
    derive it from the most-recently-modified transcript in this project's
    ~/.claude/projects/<dashed-cwd>/ directory (filename == sessionId)."""
    given = (given or "").strip()
    if given:
        return given
    import glob
    cwd = os.getcwd()
    # Claude Code stores transcripts under ~/.claude/projects/<cwd-with-/-replaced-by-->/
    # (the leading "/" of an absolute cwd becomes the single leading "-").
    dashed = cwd.replace("/", "-")
    proj_dir = os.path.join(os.path.expanduser("~/.claude/projects"), dashed)
    try:
        files = sorted(glob.glob(os.path.join(proj_dir, "*.jsonl")),
                       key=os.path.getmtime, reverse=True)
    except OSError:
        files = []
    if files:
        return os.path.basename(files[0])[:-6]  # strip .jsonl
    return ""


def normalize_todos(payload: dict) -> list[core.Todo]:
    """Read todos from TodoWrite tool_input (PreToolUse) or tool_response (PostToolUse).

    Claude Code's TodoWrite items are {content, status, activeForm, ...}. There is
    no stable per-item id, so we synthesize one from the content (the core only
    needs an id for its validation error messages)."""
    src = None
    ti = payload.get("tool_input") or {}
    if isinstance(ti, dict) and isinstance(ti.get("todos"), list):
        src = ti["todos"]
    if src is None:
        tr = payload.get("tool_response") or {}
        if isinstance(tr, dict) and isinstance(tr.get("todos"), list):
            src = tr["todos"]
        elif isinstance(tr, list):
            src = tr
    if not src:
        return []
    out = []
    for item in src:
        if not isinstance(item, dict):
            continue
        content = str(item.get("content") or "")
        out.append(core.Todo(
            id=str(item.get("id") or content[:40]),
            content=content,
            status=str(item.get("status") or ""),
        ))
    return out


def emit(action: core.HookAction | None, *, event: str, deny_as_permission: bool = False) -> None:
    """Render a core HookAction to Claude Code stdout JSON (or print nothing).

    deny_as_permission=True is for PreToolUse, which uniquely carries its deny
    decision inside hookSpecificOutput.permissionDecision (not a top-level
    decision:block).
    """
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
            "hookEventName": event or "PostToolUse",
            "additionalContext": action.additional_context,
        }
    if out:
        sys.stdout.write(json.dumps(out, ensure_ascii=False))
