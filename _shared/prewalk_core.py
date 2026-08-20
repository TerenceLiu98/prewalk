"""Prewalk shared core — agent-agnostic state machine + helpers.

Both the Claude Code and Codex adapters import this module. The adapters are
thin I/O shims that:
  1. parse the host's hook JSON on stdin (different field names per host),
  2. call into :func:`decide` (or the smaller helpers) with a normalized view,
  3. render the returned :class:`HookAction` into the host's output JSON.

The core never reads stdin / writes stdout directly and never imports a host
SDK. Zero third-party deps — standard library only.

Technique: Can Bölük / Stencil ("You only need the frontier model for one
edit"). The frontier explores, plans, and lands one verified edit. A host
adapter then hands a structured summary to a cheaper executor.
"""

from __future__ import annotations

import json
import os
import re
import secrets
import sys
import tempfile
import threading
from contextlib import contextmanager
from dataclasses import dataclass, field, asdict
from pathlib import Path
from typing import Any, Callable, Iterable

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

# Phases of the per-session state machine.
IDLE = "idle"
FRONTIER = "frontier"
PAUSED = "paused"
READY = "ready"        # first edit observed; waiting for a valid checkpoint
HANDOFF_REQUESTED = "handoff_requested"
EXECUTOR = "executor"
RESTORING = "restoring"

VERSION = "0.3.1"
DEFAULT_MAX_TODOS = 12
DEFAULT_PRESET = "code-value"
HANDOFF_MODES = ("auto", "spawn", "manual-model")

# A todo item counts as the handoff checkpoint if its content starts with the
# pause emoji (U+23F8, with or without the U+FE0F variation selector) or a
# case-sensitive "PAUSE" / "[PAUSE]" anchored to the very start. Mirrors
# opencode-prewalk's isPauseTodo exactly.
_PAUSE_RE = re.compile(r"^\[?PAUSE\b")


def is_pause_todo(content: str | None) -> bool:
    """True if a todo's content marks the prewalk handoff checkpoint."""
    c = (content or "").replace("️", "").lstrip()
    if c.startswith("⏸"):  # ⏸
        return True
    return bool(_PAUSE_RE.search(c))


def _status_open(status: str | None) -> bool:
    """A todo is "remaining work" if not completed/cancelled and not the pause marker."""
    return status not in ("completed", "cancelled")


@dataclass
class Todo:
    """Normalized todo item, host-agnostic."""
    id: str = ""
    content: str = ""
    status: str = ""  # pending | in_progress | completed | cancelled

    @property
    def is_pause(self) -> bool:
        return is_pause_todo(self.content)

    @property
    def open(self) -> bool:
        return _status_open(self.status) and not self.is_pause


def count_remaining(todos: Iterable[Todo]) -> int:
    """Number of real, unfinished todos (excludes the pause marker)."""
    return sum(1 for t in todos if t.open)


# Words that make a todo content count as "has a validation checkpoint".
_VERIFY_RE = re.compile(r"\b(?:verify|validate|test|build|check|inspect|confirm|lint)\b", re.I)


def validate_todo_list(todos: list[Todo], cap: int = DEFAULT_MAX_TODOS) -> str | None:
    """Return an error string if the todo list violates the prewalk rules, else None.

    Rules: non-empty, <= cap items, every item has id + actionable content, every
    item mentions a validation checkpoint, valid status. (Pause markers are
    exempt from the validation-word requirement.)
    """
    real = [t for t in todos if not t.is_pause]
    if not real:
        return "Prewalk requires a non-empty todo list before editing."
    if len(real) > cap:
        return f"Prewalk requires at most {cap} todo items; consolidate the plan and retry."
    for i, t in enumerate(real, 1):
        if not (t.id or "").strip() or not (t.content or "").strip():
            return f"Prewalk todo item {i} needs both an id and actionable content."
        if not _VERIFY_RE.search(t.content or ""):
            return f"Prewalk todo item {i} must include a validation checkpoint (test/build/verify/check)."
        if (t.status or "") not in ("", "pending", "in_progress", "completed", "cancelled"):
            return f"Prewalk todo item {i} has an invalid status."
    return None


def validate_checkpoint(todos: list[Todo], cap: int = DEFAULT_MAX_TODOS) -> str | None:
    """Validate the handoff invariant represented by a complete todo snapshot."""
    err = validate_todo_list(todos, cap)
    if err:
        return err
    real = [todo for todo in todos if not todo.is_pause]
    if real[0].status != "completed":
        return "Prewalk task #1 must be completed and verified before the PAUSE checkpoint."
    if not any(todo.is_pause for todo in todos):
        return "Prewalk requires a PAUSE checkpoint todo before handoff."
    return None


# ---------------------------------------------------------------------------
# Per-session state
# ---------------------------------------------------------------------------

@dataclass
class PrewalkState:
    session_id: str
    phase: str = FRONTIER
    preset: str = DEFAULT_PRESET
    max_todos: int = DEFAULT_MAX_TODOS
    auto_swap: bool = False          # --no-pause: swap without waiting for /pw-go
    pause_seen: bool = False
    frontier_todos_ever_seen: bool = False
    todos_remaining: int = 0
    blocked_edits: int = 0
    first_edit_landed: bool = False  # frontier has completed its one verified edit
    checkpoint_evidence: str = ""   # observed-edit | todo-only
    checkpoint_warning: str = ""
    handoff_done: bool = False       # a handoff was confirmed successful
    handoff_host: str = ""
    handoff_mode: str = "auto"
    require_model_routing: bool = True
    handoff_routed: bool = False
    handoff_token: str = ""
    handoff_tool_use_id: str = ""
    executor_agent_id: str = ""
    handoff_launch_acknowledged: bool = False
    handoff_attempts: int = 0
    last_handoff_error: str = ""
    original_model: str = ""         # planner model, to restore after executor finishes
    executor_model: str = ""         # model the /pw-go handoff should switch to
    planner_thinking: str = ""
    executor_thinking: str = ""
    created_turn: int = 0            # informational

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "PrewalkState":
        known = {f for f in cls.__dataclass_fields__}  # type: ignore[attr-defined]
        return cls(**{k: v for k, v in d.items() if k in known})


# ---------------------------------------------------------------------------
# State store (JSON file per host; one active session at a time is the norm,
# but we key by session_id to be safe across resumed sessions).
# ---------------------------------------------------------------------------

_LOCK = threading.Lock()


def state_path(store_file: str | os.PathLike[str]) -> Path:
    p = Path(store_file)
    p.parent.mkdir(parents=True, exist_ok=True)
    return p


@contextmanager
def _store_lock(store_file: str | os.PathLike[str]):
    """Serialize state transactions across threads and hook processes."""
    with _LOCK:
        p = state_path(store_file)
        lock_path = p.with_name(p.name + ".lock")
        with open(lock_path, "a+b") as lock_file:
            if os.name == "nt":
                import msvcrt

                lock_file.seek(0, os.SEEK_END)
                if lock_file.tell() == 0:
                    lock_file.write(b"\0")
                    lock_file.flush()
                lock_file.seek(0)
                msvcrt.locking(lock_file.fileno(), msvcrt.LK_LOCK, 1)
            else:
                import fcntl

                fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX)
            try:
                yield
            finally:
                lock_file.seek(0)
                if os.name == "nt":
                    msvcrt.locking(lock_file.fileno(), msvcrt.LK_UNLCK, 1)
                else:
                    fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)


def _quarantine_corrupt_store(store_file: str | os.PathLike[str]) -> None:
    """Move malformed state aside so the next update can start cleanly."""
    p = Path(store_file)
    backup = p.with_name(p.name + ".corrupt")
    try:
        os.replace(p, backup)
    except FileNotFoundError:
        pass


def _read_all(store_file: str | os.PathLike[str]) -> dict[str, dict[str, Any]]:
    try:
        with open(store_file, "r", encoding="utf-8") as fh:
            data = json.load(fh)
    except FileNotFoundError:
        return {}
    except (json.JSONDecodeError, UnicodeDecodeError):
        _quarantine_corrupt_store(store_file)
        return {}
    if not isinstance(data, dict) or any(not isinstance(value, dict) for value in data.values()):
        _quarantine_corrupt_store(store_file)
        return {}
    return data


def _write_all(store_file: str | os.PathLike[str], data: dict[str, dict[str, Any]]) -> None:
    p = state_path(store_file)
    fd, tmp_name = tempfile.mkstemp(prefix=p.name + ".", suffix=".tmp", dir=p.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(data, fh, indent=2)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp_name, p)
    finally:
        try:
            os.unlink(tmp_name)
        except FileNotFoundError:
            pass


def load_state(store_file: str | os.PathLike[str], session_id: str) -> PrewalkState | None:
    if not session_id:
        return None
    with _store_lock(store_file):
        rec = _read_all(store_file).get(session_id)
    return PrewalkState.from_dict(rec) if rec else None


def save_state(store_file: str | os.PathLike[str], state: PrewalkState) -> None:
    with _store_lock(store_file):
        data = _read_all(store_file)
        data[state.session_id] = state.to_dict()
        _write_all(store_file, data)


def clear_state(store_file: str | os.PathLike[str], session_id: str) -> None:
    with _store_lock(store_file):
        data = _read_all(store_file)
        if data.pop(session_id, None) is not None:
            _write_all(store_file, data)


# ---------------------------------------------------------------------------
# Preset loading (tiny hand-rolled parsers — JSON for Claude Code, a minimal
# TOML subset for Codex; we avoid a PyYAML/tomli dependency.)
# ---------------------------------------------------------------------------

@dataclass
class Preset:
    name: str
    planner_model: str
    executor_model: str
    description: str = ""
    max_todos: int = DEFAULT_MAX_TODOS
    planner_thinking: str = ""
    executor_thinking: str = ""
    handoff_mode: str = "auto"
    require_model_routing: bool = True


def load_presets_json(path: str | os.PathLike[str]) -> dict[str, Preset]:
    """Claude Code presets live in JSON. Schema:
    { "default": "code-value",
      "presets": { "<name>": { "planner": "...", "executor": "...", "description": "...",
                               "max_todos": 12 }, ... } }
    """
    try:
        with open(path, "r", encoding="utf-8") as fh:
            data = json.load(fh)
    except (FileNotFoundError, json.JSONDecodeError):
        return {}
    out: dict[str, Preset] = {}
    for name, raw in (data.get("presets") or {}).items():
        if not isinstance(raw, dict):
            continue
        planner = str(raw.get("planner") or "").strip()
        executor = str(raw.get("executor") or "").strip()
        if not planner or not executor:
            continue
        out[name] = Preset(
            name=name,
            planner_model=planner,
            executor_model=executor,
            description=str(raw.get("description") or ""),
            max_todos=int(raw.get("max_todos") or DEFAULT_MAX_TODOS),
            planner_thinking=str(raw.get("planner_thinking") or "").strip(),
            executor_thinking=str(raw.get("executor_thinking") or "").strip(),
            handoff_mode=_handoff_mode(raw.get("handoff_mode")),
            require_model_routing=bool(raw.get("require_model_routing", True)),
        )
    return out


# Minimal TOML reader sufficient for our presets.example.toml (flat [presets.NAME]
# tables with string/int values). We intentionally do not implement the full
# TOML spec.
_TOML_STRING_RE = re.compile(r'^"((?:[^"\\]|\\.)*)"$')
_TOML_TABLE_RE = re.compile(r'^\[presets\.([A-Za-z0-9_-]+)\]\s*$')


def load_presets_toml(path: str | os.PathLike[str]) -> dict[str, Preset]:
    """Codex presets live in TOML. Recognized shape:
    default_preset = "code-value"
    [presets.code-value]
    description = "..."
    planner = "gpt-5.6-sol"      # optionally "model @ effort"
    executor = "gpt-5.6-luna"
    max_todos = 12
    """
    try:
        text = Path(path).read_text(encoding="utf-8")
    except FileNotFoundError:
        return {}
    out: dict[str, Preset] = {}
    current: str | None = None
    bucket: dict[str, Any] = {}
    for raw_line in text.splitlines():
        line = raw_line.split("#", 1)[0].strip()
        if not line:
            continue
        m = _TOML_TABLE_RE.match(line)
        if m:
            if current is not None:
                _flush_preset(out, current, bucket)
            current = m.group(1)
            bucket = {}
            continue
        if current is not None and "=" in line:
            key, _, val = line.partition("=")
            key = key.strip()
            val = val.strip()
            sm = _TOML_STRING_RE.match(val)
            if sm:
                bucket[key] = sm.group(1).encode().decode("unicode_escape")
            elif val.isdigit():
                bucket[key] = int(val)
            elif val.lower() in ("true", "false"):
                bucket[key] = val.lower() == "true"
    if current is not None:
        _flush_preset(out, current, bucket)
    return out


def _flush_preset(out: dict[str, Preset], name: str, bucket: dict[str, Any]) -> None:
    planner = str(bucket.get("planner") or "").strip()
    executor = str(bucket.get("executor") or "").strip()
    if not planner or not executor:
        return
    out[name] = Preset(
        name=name,
        planner_model=planner,
        executor_model=executor,
        description=str(bucket.get("description") or ""),
        max_todos=int(bucket.get("max_todos") or DEFAULT_MAX_TODOS),
        planner_thinking=str(bucket.get("planner_thinking") or "").strip(),
        executor_thinking=str(bucket.get("executor_thinking") or "").strip(),
        handoff_mode=_handoff_mode(bucket.get("handoff_mode")),
        require_model_routing=bool(bucket.get("require_model_routing", True)),
    )


def _handoff_mode(value: Any) -> str:
    mode = str(value or "auto").strip()
    return mode if mode in HANDOFF_MODES else "auto"


def default_preset_json(path: str | os.PathLike[str]) -> str:
    try:
        with open(path, "r", encoding="utf-8") as fh:
            return str(json.load(fh).get("default") or DEFAULT_PRESET)
    except (FileNotFoundError, json.JSONDecodeError):
        return DEFAULT_PRESET


def default_preset_toml(path: str | os.PathLike[str]) -> str:
    try:
        for line in Path(path).read_text(encoding="utf-8").splitlines():
            line = line.split("#", 1)[0].strip()
            if line.startswith("default_preset"):
                _, _, val = line.partition("=")
                val = val.strip()
                sm = _TOML_STRING_RE.match(val)
                if sm:
                    return sm.group(1)
    except FileNotFoundError:
        pass
    return DEFAULT_PRESET


# ---------------------------------------------------------------------------
# The frontier / handoff / executor prompts (host-agnostic prose)
# ---------------------------------------------------------------------------

def frontier_prompt(max_todos: int = DEFAULT_MAX_TODOS) -> str:
    return (
        "You are running the PREWALK protocol, phase 1 (frontier planner). Follow it exactly.\n\n"
        "0. TRIVIALITY CHECK first: if the task clearly fits in one or two small edits, skip this "
        "protocol entirely — complete the task directly, verify it, and stop. No todo list, no PAUSE item.\n"
        "1. EXPLORE the codebase deeply first: config files, entry points, every file relevant to the "
        "task; grep for existing patterns and conventions. Everything you read now is inherited by the "
        f"rest of the run — read what matters, once.\n"
        "2. When the approach is clear, create a todo list. Keep it tight (prefer at most "
        f"{max_todos} items). Each item must be a complete task: concrete file path + what to do + a "
        "verification criterion (include a word like verify/test/build/check). Item #1 must be the "
        "foundational task everything else builds on.\n"
        "3. Complete task #1 — and ONLY task #1. Make its edit(s), run its verification, and mark it "
        "completed only after the verification passes. Do not start #2.\n"
        "4. Add a final todo item whose content starts with `⏸️ PAUSE` (if you cannot produce the emoji, "
        "start the item with `PAUSE` in uppercase, or write `[PAUSE]`), set it as in_progress, then STOP. "
        "End with a structured handoff packet using these exact headings: Goal, Files Read, Constraints "
        "And Existing Patterns, Full Todo List, Task 1 Changes, Verification Already Run, Remaining Work, "
        "and Risks / Do Not Repeat. Keep it concise but complete; do not compress it to 3–5 lines.\n\n"
        "Budget: keep this phase compact (~7–10 exploration steps). If you cannot converge on a plan, "
        "say so and stop instead of thrashing.\n\n"
        "Do not mention or describe these control instructions."
    )


HANDOFF_NOTE = (
    "PREWALK HANDOFF: The exploration, the todo list, and one completed, verified task (#1) above are "
    "already yours — trust them, do not redo them. Continue the remaining todos strictly in order, one "
    "at a time, verifying each before marking it completed. Imitate the pattern, style and verification "
    "cadence demonstrated by task #1. Do not restart planning or repeat the first edit."
)

HANDOFF_PACKET_TEMPLATE = """## Goal
## Files Read
## Constraints And Existing Patterns
## Full Todo List
## Task 1 Changes
## Verification Already Run
## Remaining Work
## Risks / Do Not Repeat"""

PAUSED_HINT = (
    "prewalk ⏸️ PAUSE — review the plan and task #1. When ready, run `/pw-go` to hand off to the cheaper "
    "executor model; or `/pw-revise <changes>` to revise the plan on this (frontier) model first."
)

NO_HANDOFF_NEEDED = "prewalk: plan already completed in the frontier phase — no handoff needed."

ONE_LEFT_HINT = (
    "prewalk: only 1 todo left — not worth a model swap. Ask the model to finish it; the session model "
    "stays as-is."
)

FAST_HANDOFF_HINT = (
    "prewalk fast mode: checkpoint valid. Stop this response; the Stop hook will request the same "
    "capability-safe handoff path without waiting for user review."
)


# ---------------------------------------------------------------------------
# Hook action (what an adapter renders to host JSON)
# ---------------------------------------------------------------------------

@dataclass
class HookAction:
    """Normalized decision an adapter renders into host-specific output."""
    proceed: bool = True            # False => block the tool/stop the turn
    block_reason: str = ""          # fed back to the model when proceed is False
    additional_context: str = ""    # injected alongside the prompt/tool result
    system_message: str = ""        # shown to the user (not the model)
    # Adapters may set host-specific fields; core only fills the above.
    extra: dict[str, Any] = field(default_factory=dict)

    def to_debug(self) -> str:
        return json.dumps(
            {"proceed": self.proceed, "reason": self.block_reason,
             "ctx": bool(self.additional_context), "msg": self.system_message},
            ensure_ascii=False,
        )


# ---------------------------------------------------------------------------
# Decision logic
# ---------------------------------------------------------------------------

def start_run(store_file: str | os.PathLike[str], session_id: str, preset: Preset,
              auto_swap: bool, turn: int = 0) -> PrewalkState:
    """Arm a new prewalk run for this session (replaces any existing one)."""
    state = PrewalkState(
        session_id=session_id,
        phase=FRONTIER,
        preset=preset.name,
        max_todos=preset.max_todos,
        auto_swap=auto_swap,
        original_model=preset.planner_model,
        executor_model=preset.executor_model,
        planner_thinking=preset.planner_thinking,
        executor_thinking=preset.executor_thinking,
        handoff_mode=preset.handoff_mode,
        require_model_routing=preset.require_model_routing,
        created_turn=turn,
    )
    save_state(store_file, state)
    return state


def on_todos_changed(
    store_file: str | os.PathLike[str],
    session_id: str,
    todos: list[Todo],
    *,
    on_swap: "Callable[[PrewalkState], None] | None" = None,
) -> HookAction | None:
    """Called when the todo list is updated (PostToolUse on the todo tool, or a
    polling Stop hook). Drives the whole frontier→paused→executor machine.

    Returns a HookAction the adapter renders, or None if prewalk has nothing to
    say. ``on_swap`` (if provided and auto_swap is True) is the host hook that
    actually performs the model switch + kickoff — kept out of core so it stays
    agent-agnostic.
    """
    state = load_state(store_file, session_id)
    if state is None or state.phase == IDLE:
        return None
    if not todos:
        return None

    pause_present = any(t.is_pause for t in todos)
    if pause_present:
        state.pause_seen = True
    if state.phase == FRONTIER:
        state.frontier_todos_ever_seen = True
    remaining = count_remaining(todos)
    state.todos_remaining = remaining

    # Executor completion: all real todos done. Clear + tell user to restore.
    if state.phase == EXECUTOR:
        if remaining == 0:
            clear_state(store_file, session_id)
            return HookAction(
                system_message="prewalk: all todos completed ✅ — run `/model " + state.original_model
                               + "` to restore your planner model.",
            )
        save_state(store_file, state)
        return None

    # Below: frontier / paused checkpoint logic — only meaningful when the pause
    # marker is part of this update.
    if not pause_present:
        save_state(store_file, state)
        return None

    checkpoint_error = validate_checkpoint(todos, cap=_preset_cap(state, len(todos)))
    if checkpoint_error:
        state.checkpoint_warning = checkpoint_error
        save_state(store_file, state)
        return HookAction(system_message="prewalk: checkpoint rejected — " + checkpoint_error)

    if remaining == 0:
        clear_state(store_file, session_id)
        return HookAction(system_message=NO_HANDOFF_NEEDED)
    if remaining == 1:
        # One todo left isn't worth a model swap. Disarm the paused-phase path.
        state.phase = EXECUTOR  # arms completion detection; no swap performed
        save_state(store_file, state)
        return HookAction(system_message=ONE_LEFT_HINT)

    state.phase = PAUSED
    state.checkpoint_evidence = "observed-edit" if state.first_edit_landed else "todo-only"
    state.checkpoint_warning = "" if state.first_edit_landed else (
        "The host did not observe task #1's edit. Review its diff and verification before handoff."
    )
    save_state(store_file, state)
    hint = FAST_HANDOFF_HINT if state.auto_swap else PAUSED_HINT
    if state.checkpoint_warning:
        hint += " Warning: " + state.checkpoint_warning
    return HookAction(additional_context=FAST_HANDOFF_HINT if state.auto_swap else "", system_message=hint)


def _preset_cap(state: PrewalkState, fallback: int) -> int:
    # max_todos was not persisted before v0.3; a conservative default keeps old
    # state files readable while new runs use the configured cap.
    return int(getattr(state, "max_todos", 0) or DEFAULT_MAX_TODOS or fallback)


def on_pw_go(store_file: str | os.PathLike[str], session_id: str, *, host: str = "claude") -> HookAction:
    """`/pw-go` was invoked — user confirms the handoff.

    Valid in the frontier or ready phase (the run is armed and the frontier has
    done its part). Both hosts hand off by spawning a fresh-context executor
    subagent pinned to the executor model, but the *mechanism* differs, so the
    handoff note is host-specific:

    - ``host="claude"``: the model spawns ONE Task (Agent tool); the host's
      PreToolUse ``handoff_router`` hook rewrites that spawn onto the executor
      model automatically.
    - ``host="codex"``: Codex has no ``updatedInput``, so no hook can rewrite
      the spawn. The model calls the native ``spawn_agent`` tool with an
      explicit model and a fresh-context flag.

    Returns a no-checkpoint message if nothing is armed."""
    state = load_state(store_file, session_id)
    if state is None:
        return HookAction(
            additional_context=(
                "There is no active prewalk checkpoint in this session. Reply with a single line "
                "saying so and end your turn — do not touch the todo list or any file."
            )
        )
    if state.handoff_done or state.phase == EXECUTOR:
        return HookAction(
            additional_context=(
                "Prewalk already handed off to the executor. Continue the remaining work in the "
                "executor subagent; do not spawn another handoff."
            )
        )
    if state.phase == HANDOFF_REQUESTED:
        return HookAction(
            additional_context=(
                "A prewalk handoff is already pending confirmation. Confirm it after a successful spawn, "
                "or mark it failed so the checkpoint becomes retryable."
            )
        )
    if state.phase != PAUSED:
        return HookAction(
            additional_context=(
                "There is no active prewalk checkpoint in this session. Reply with a single line "
                "saying so and end your turn — do not touch the todo list or any file."
            )
        )
    state.phase = HANDOFF_REQUESTED
    state.handoff_host = host
    state.handoff_attempts += 1
    state.handoff_routed = False
    state.handoff_token = secrets.token_urlsafe(24) if host == "claude" else ""
    state.handoff_tool_use_id = ""
    state.executor_agent_id = ""
    state.handoff_launch_acknowledged = False
    state.last_handoff_error = ""
    save_state(store_file, state)

    if host == "codex" and state.handoff_mode == "manual-model":
        action_line = (
            f"ACTION: do not spawn a subagent. Ask the user to run `/model {state.executor_model}`"
            + (f" with thinking `{state.executor_thinking}`" if state.executor_thinking else "")
            + ", then run the `pw-resume` skill. Only `pw-resume` confirms the handoff."
        )
        sysmsg = f"prewalk: manual model handoff requested for {state.executor_model}."
    elif host == "codex":
        task_name = f"prewalk_executor_{state.handoff_attempts}"
        action_line = (
            f"ACTION: inspect the native `spawn_agent` schema, then call it exactly once with "
            f"`task_name=\"{task_name}\"`, the structured Handoff Packet as `message`, "
            f"`fork_turns=\"none\"`, and `model=\"{state.executor_model}\"`. "
            + (f"Include `reasoning_effort=\"{state.executor_thinking}\"` only if the schema supports it. "
               if state.executor_thinking else "")
            + "After the tool returns success, run `_pw.py confirm`; if it fails, run `_pw.py fail <reason>`. "
            + (f"Because `require_model_routing` is true, do not spawn without a model parameter; use the "
               f"manual `/model {state.executor_model}` + `pw-resume` fallback instead."
               if state.require_model_routing else
               "If model routing is unavailable, spawning on the runtime-selected model is allowed by this preset.")
        )
        sysmsg = f"prewalk: capability-safe handoff requested for {state.executor_model}."
    else:
        action_line = (
            f"ACTION: spawn ONE Task whose prompt contains the structured Handoff Packet and this exact "
            f"line: `PREWALK_HANDOFF_TOKEN: {state.handoff_token}`. The prewalk hook will route it onto "
            f"{state.executor_model}. Agent PostToolUse only acknowledges launch; the bound SubagentStop "
            f"event records the final marker. Do not switch models yourself or do the remaining edits here."
        )
        sysmsg = f"prewalk: handoff requested — spawn a Task for the remaining work (executor {state.executor_model})."
    return HookAction(
        additional_context=f"{HANDOFF_NOTE}\n\nRequired packet:\n{HANDOFF_PACKET_TEMPLATE}\n\n{action_line}",
        system_message=sysmsg,
    )


def on_handoff_confirm(store_file: str | os.PathLike[str], session_id: str) -> HookAction:
    state = load_state(store_file, session_id)
    if state is None or state.phase != HANDOFF_REQUESTED:
        return HookAction(additional_context="No pending prewalk handoff can be confirmed.")
    state.phase = EXECUTOR
    state.handoff_done = True
    state.handoff_routed = True
    state.last_handoff_error = ""
    save_state(store_file, state)
    return HookAction(system_message=f"prewalk: handoff confirmed on {state.executor_model}.")


def on_handoff_launch_ack(
    store_file: str | os.PathLike[str], session_id: str, tool_use_id: str
) -> HookAction | None:
    """Record success of the exact routed Agent call without treating it as completion."""
    state = load_state(store_file, session_id)
    if (
        state is None
        or state.phase not in (HANDOFF_REQUESTED, EXECUTOR)
        or not state.handoff_routed
        or not tool_use_id
        or tool_use_id != state.handoff_tool_use_id
    ):
        return None
    if state.handoff_launch_acknowledged:
        return None
    state.handoff_launch_acknowledged = True
    save_state(store_file, state)
    return HookAction(system_message="prewalk: executor launch acknowledged; waiting for its lifecycle result.")


def on_executor_started(
    store_file: str | os.PathLike[str], session_id: str, agent_id: str
) -> HookAction | None:
    """Bind the one routed Claude executor and enter the executor phase."""
    state = load_state(store_file, session_id)
    if (
        state is None
        or state.phase not in (HANDOFF_REQUESTED, EXECUTOR)
        or not state.handoff_routed
        or not state.handoff_tool_use_id
        or not agent_id
    ):
        return None
    if state.executor_agent_id:
        return None
    state.executor_agent_id = agent_id
    state.phase = EXECUTOR
    state.handoff_done = True
    state.last_handoff_error = ""
    save_state(store_file, state)
    return HookAction(system_message=f"prewalk: executor {agent_id} started on {state.executor_model}.")


def on_handoff_failed(
    store_file: str | os.PathLike[str], session_id: str, reason: str = "handoff failed"
) -> HookAction:
    state = load_state(store_file, session_id)
    if state is None or state.phase != HANDOFF_REQUESTED:
        return HookAction(additional_context="No pending prewalk handoff can be failed.")
    state.phase = PAUSED
    state.handoff_done = False
    state.handoff_routed = False
    state.handoff_token = ""
    state.handoff_tool_use_id = ""
    state.executor_agent_id = ""
    state.handoff_launch_acknowledged = False
    state.last_handoff_error = reason.strip() or "handoff failed"
    save_state(store_file, state)
    return HookAction(system_message="prewalk: handoff failed; checkpoint restored and `/pw-go` is retryable.")


def on_executor_result(
    store_file: str | os.PathLike[str], session_id: str, *, complete: bool, detail: str = ""
) -> HookAction:
    state = load_state(store_file, session_id)
    if state is None:
        return HookAction(additional_context="No active prewalk executor run was found.")
    if state.phase != EXECUTOR:
        return HookAction(additional_context="Prewalk cannot record an executor result before handoff confirmation.")
    if complete:
        planner = state.original_model
        clear_state(store_file, session_id)
        return HookAction(system_message=f"prewalk: executor completed all work. Restore `/model {planner}` if needed.")
    state.phase = PAUSED
    state.handoff_done = False
    state.handoff_routed = False
    state.handoff_token = ""
    state.handoff_tool_use_id = ""
    state.executor_agent_id = ""
    state.handoff_launch_acknowledged = False
    state.last_handoff_error = detail.strip() or "executor stopped with work remaining"
    save_state(store_file, state)
    return HookAction(system_message="prewalk: executor incomplete; checkpoint restored for `/pw-go` or `/pw-revise`.")


def on_pw_revise(store_file: str | os.PathLike[str], session_id: str, revision: str) -> HookAction:
    """`/pw-revise <changes>` — keep the frontier model, fold the revision in."""
    state = load_state(store_file, session_id)
    if state is None or state.phase != PAUSED:
        return HookAction(
            additional_context=(
                "There is no active prewalk checkpoint to revise. Reply with a single line saying so "
                "and end your turn."
            )
        )
    # Stay paused; the frontier agent re-adds the ⏸️ checkpoint after revising.
    state.last_handoff_error = ""
    save_state(store_file, state)
    return HookAction(
        additional_context=(
            f"PREWALK REVISION: update the plan accordingly: {revision or '(no detail given)'}. "
            "Re-explore only what the revision affects, fix the todo list, re-verify task #1 if it "
            "changed, then re-add the `⏸️ PAUSE` checkpoint todo and stop again for confirmation."
        ),
        system_message="prewalk: plan revised on the frontier — review and `/pw-go` when ready.",
    )


def on_edit_attempt(
    store_file: str | os.PathLike[str],
    session_id: str,
    todos: list[Todo],
    cap: int = DEFAULT_MAX_TODOS,
) -> HookAction:
    """PreToolUse gate on an edit tool. Blocks edits during the frontier phase
    until a valid capped todo list exists; disarms after a second violation."""
    state = load_state(store_file, session_id)
    if state is None or state.phase != FRONTIER:
        return HookAction()  # not arming — allow

    err = validate_todo_list(todos, cap)
    if err is None:
        return HookAction()  # valid todo list present — allow

    state.blocked_edits += 1
    if state.blocked_edits < 2:
        save_state(store_file, state)
        return HookAction(
            proceed=False,
            block_reason=err + " Create the todo list (with a validation checkpoint on every item) "
                               "before editing.",
        )
    # Second violation: restore + disarm to avoid a loop.
    clear_state(store_file, session_id)
    return HookAction(
        proceed=False,
        block_reason=(
            "Prewalk disarmed after a second edit attempt without the required todo list. Create a "
            "valid todo list and re-run `/prewalk` if you still want the handoff."
        ),
    )


def on_turn_end(store_file: str | os.PathLike[str], session_id: str) -> HookAction | None:
    """Stop hook. If frontier finished without ever emitting a todo list, this
    is the trivial path — close cleanly. If it emitted todos but never the ⏸️
    checkpoint, warn once and close (anomaly). Otherwise no-op (the paused
    checkpoint is driven by todo updates, handled elsewhere)."""
    state = load_state(store_file, session_id)
    if state is None:
        return None
    if state.phase == FRONTIER and not state.frontier_todos_ever_seen:
        clear_state(store_file, session_id)
        return HookAction(system_message="prewalk: trivial task — protocol not engaged.")
    if state.phase == FRONTIER and state.frontier_todos_ever_seen and not state.pause_seen:
        clear_state(store_file, session_id)
        return HookAction(
            system_message="prewalk: checkpoint todo (⏸️ PAUSE) not detected — check the todo format.",
        )
    return None


def on_fast_handoff(
    store_file: str | os.PathLike[str], session_id: str, *, host: str
) -> HookAction | None:
    """Request one automatic handoff from a validated fast-mode checkpoint."""
    state = load_state(store_file, session_id)
    if state is None or state.phase != PAUSED or not state.auto_swap:
        return None
    requested = on_pw_go(store_file, session_id, host=host)
    return HookAction(
        proceed=False,
        block_reason=requested.additional_context,
        system_message=requested.system_message,
    )


# ---------------------------------------------------------------------------
# Host-agnostic state inspection (for /prewalk status)
# ---------------------------------------------------------------------------

def describe(store_file: str | os.PathLike[str], session_id: str) -> str:
    state = load_state(store_file, session_id)
    if state is None:
        return "prewalk: idle (no armed run in this session)."
    return (
        f"prewalk {VERSION}: {state.phase} [{state.preset}]\n"
        f"  planner: {state.original_model} ({state.planner_thinking or 'default'})"
        f"  ->  executor: {state.executor_model} ({state.executor_thinking or 'default'})\n"
        f"  handoff_mode: {state.handoff_mode}; require_model_routing: "
        f"{'yes' if state.require_model_routing else 'no'}; attempts: {state.handoff_attempts}; "
        f"routed: {'yes' if state.handoff_routed else 'no'}\n"
        f"  route_tool: {state.handoff_tool_use_id or 'none'}; executor_agent: "
        f"{state.executor_agent_id or 'none'}; launch_ack: "
        f"{'yes' if state.handoff_launch_acknowledged else 'no'}\n"
        f"  fast: {'yes' if state.auto_swap else 'no'}; evidence: {state.checkpoint_evidence or 'none'}; "
        f"todos_remaining: {state.todos_remaining}; last_error: {state.last_handoff_error or 'none'}"
    )


def disarm(store_file: str | os.PathLike[str], session_id: str) -> str:
    state = load_state(store_file, session_id)
    if state is None:
        return "prewalk was not armed for this session."
    clear_state(store_file, session_id)
    return f"prewalk disarmed. Restore your model with `/model {state.original_model}` if needed."


# ---------------------------------------------------------------------------
# Small CLI for manual inspection: python3 prewalk_core.py status <store> <sid>
# ---------------------------------------------------------------------------

def _cli() -> int:
    if len(sys.argv) >= 4 and sys.argv[1] in ("status", "disarm", "clear"):
        store, sid = sys.argv[2], sys.argv[3]
        if sys.argv[1] == "status":
            print(describe(store, sid))
        elif sys.argv[1] == "disarm":
            print(disarm(store, sid))
        else:
            clear_state(store, sid)
            print("cleared")
        return 0
    print("usage: prewalk_core.py status|disarm|clear <store_file> <session_id>", file=sys.stderr)
    return 2


if __name__ == "__main__":
    raise SystemExit(_cli())
