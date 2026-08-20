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
import re
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


def claude_commands(text: str) -> str:
    """Render shared-core command names through the plugin skill namespace."""
    text = text.replace("/pw-", "/prewalk:pw-")
    return re.sub(r"(?<![A-Za-z0-9_.~-])/prewalk(?!:)", "/prewalk:prewalk", text)


def read_input() -> dict:
    raw = sys.stdin.read()
    if not raw.strip():
        return {}
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        return {}


def session_id(payload: dict, *, allow_subagent: bool = False) -> str:
    if not allow_subagent and (payload.get("agent_id") or payload.get("agentId")):
        return ""
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


def _task_to_todo(item: dict) -> core.Todo | None:
    """Coerce one task dict (TodoWrite OR the newer TaskCreate/TaskList shape) to a core.Todo.

    TodoWrite items: {content, status, activeForm}
    New task system: {id/uuid, subject, description, status, ...}"""
    if not isinstance(item, dict):
        return None
    content = str(
        item.get("content")
        or item.get("subject")
        or item.get("description")
        or item.get("text")
        or item.get("step")
        or item.get("title")
        or ""
    )
    status = str(item.get("status") or item.get("state") or "").lower()
    # New system uses status values like "pending"/"in_progress"/"completed"; same vocabulary.
    return core.Todo(
        id=str(item.get("id") or item.get("uuid") or content[:40]),
        content=content,
        status=status,
    )


def normalize_todos(payload: dict) -> list[core.Todo]:
    """Read the current task list from a tool event. Supports BOTH:

    - TodoWrite: tool_input.todos = [{content, status, activeForm}, ...]
    - New task system (TaskCreate/TaskUpdate/TaskList): tool_response may carry
      a list of task objects, or a single created/updated task. We prefer a list
      in tool_response (TaskList, or TodoWrite's echo); else fall back to a
      single-item list from tool_input (TaskCreate)."""
    out: list[core.Todo] = []

    # 1) A list of items in tool_input (TodoWrite) or tool_response (TaskList).
    for holder in (
        _event_part(payload, "tool_input", "toolInput"),
        _event_part(payload, "tool_response", "toolResponse"),
    ):
        items = _todo_items(holder)
        if items:
            for it in items:
                t = _task_to_todo(it) if isinstance(it, dict) else None
                if t:
                    out.append(t)
            if out:
                return out  # a full list beats a single item

    # 2) Single created/updated task (TaskCreate / TaskUpdate tool_input).
    ti = _event_part(payload, "tool_input", "toolInput") or {}
    if isinstance(ti, dict) and (ti.get("subject") or ti.get("description")):
        t = _task_to_todo(ti)
        if t:
            # TaskCreate implies a new pending item; TaskUpdate carries a status.
            if not t.status:
                t.status = "pending"
            out.append(t)
    return out


def normalize_edit_success(payload: dict) -> bool:
    """Return whether a PostToolUse edit payload represents a successful edit."""
    response = _event_part(payload, "tool_response", "toolResponse")
    if response is None or response is False:
        return False
    return not _has_explicit_failure(response)


def _tool_name(payload: dict) -> str:
    raw = payload.get("tool_name") or payload.get("toolName") or payload.get("name") or ""
    return str(raw).rsplit(".", 1)[-1].lower()


def _command_heads(command: str) -> list[str]:
    """Return executable-position words while ignoring quotes and comments."""
    heads: list[str] = []
    word: list[str] = []
    quote = ""
    escaped = False
    expect_head = True

    def finish_word() -> None:
        nonlocal expect_head
        if not word:
            return
        token = "".join(word)
        word.clear()
        if expect_head and "=" not in token:
            heads.append(token)
            expect_head = False

    index = 0
    while index < len(command):
        char = command[index]
        if escaped:
            word.append(char)
            escaped = False
        elif quote:
            if char == quote:
                quote = ""
            elif char == "\\" and quote == '"':
                escaped = True
            else:
                word.append(char)
        elif char in ("'", '"'):
            quote = char
        elif char == "\\":
            escaped = True
        elif char == "#" and not word:
            finish_word()
            while index < len(command) and command[index] != "\n":
                index += 1
            expect_head = True
        elif char.isspace():
            finish_word()
            if char == "\n":
                expect_head = True
        elif char in ";|&()":
            finish_word()
            expect_head = True
        else:
            word.append(char)
        index += 1
    finish_word()
    return heads


def _shell_applies_patch(payload: dict) -> bool:
    tool_input = _event_part(payload, "tool_input", "toolInput") or {}
    if not isinstance(tool_input, dict):
        return False
    command = tool_input.get("cmd") or tool_input.get("command") or ""
    return any(head.rsplit("/", 1)[-1] == "apply_patch" for head in _command_heads(str(command)))


def _repoprompt_mutates(payload: dict) -> bool:
    tool_input = _event_part(payload, "tool_input", "toolInput") or {}
    if not isinstance(tool_input, dict):
        return False
    operation = str(
        tool_input.get("tool") or tool_input.get("tool_name") or tool_input.get("toolName") or ""
    ).lower()
    if operation in ("apply_edits", "apply_patch"):
        return True
    if operation != "file_actions":
        return False
    actions = tool_input.get("actions") or tool_input.get("args") or tool_input.get("arguments") or []
    text = json.dumps(actions, ensure_ascii=True).lower()
    return any(action in text for action in ('"create"', '"delete"', '"move"', '"write"'))


def _has_explicit_noop(value) -> bool:
    if isinstance(value, list):
        return any(_has_explicit_noop(item) for item in value)
    if not isinstance(value, dict):
        return False
    if any(key in value and value.get(key) is False for key in ("changed", "modified", "applied")):
        return True
    if str(value.get("status", "")).lower() in ("noop", "no-op", "no_changes", "unchanged"):
        return True
    return any(
        _has_explicit_noop(value.get(key))
        for key in ("result", "output", "data", "structured_content", "structuredContent")
    )


def normalize_mutation_success(payload: dict) -> bool:
    """True only for a successful tool call that can actually mutate files."""
    if not normalize_edit_success(payload):
        return False
    response = _event_part(payload, "tool_response", "toolResponse")
    if _has_explicit_noop(response):
        return False
    name = _tool_name(payload)
    if not name:
        return True  # Hook matchers already scoped legacy payloads to edit tools.
    if name in ("apply_patch", "edit", "write", "multiedit"):
        return True
    if name in ("bash", "exec", "exec_command"):
        return _shell_applies_patch(payload)
    if name in ("rp", "repoprompt"):
        return _repoprompt_mutates(payload)
    return False


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
    """Render a core HookAction to Claude Code stdout JSON (or print nothing).

    deny_as_permission=True is for PreToolUse, which uniquely carries its deny
    decision inside hookSpecificOutput.permissionDecision (not a top-level
    decision:block).
    """
    if action is None:
        return
    out: dict = {}
    if action.system_message:
        out["systemMessage"] = claude_commands(action.system_message)
    if not action.proceed:
        if deny_as_permission:
            out["hookSpecificOutput"] = {
                "hookEventName": event or "PreToolUse",
                "permissionDecision": "deny",
                "permissionDecisionReason": claude_commands(action.block_reason),
            }
        else:
            out["decision"] = "block"
            out["reason"] = claude_commands(action.block_reason)
    elif action.additional_context:
        out["hookSpecificOutput"] = {
            "hookEventName": event or "PostToolUse",
            "additionalContext": claude_commands(action.additional_context),
        }
    if out:
        sys.stdout.write(json.dumps(out, ensure_ascii=False))
