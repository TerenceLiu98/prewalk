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
import hashlib
import os
import re
import secrets
import sys
import tempfile
import threading
from contextlib import contextmanager
from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone
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


def _read_all_with_status(
    store_file: str | os.PathLike[str],
) -> tuple[dict[str, dict[str, Any]], str]:
    try:
        with open(store_file, "r", encoding="utf-8") as fh:
            data = json.load(fh)
    except FileNotFoundError:
        return {}, "missing"
    except (json.JSONDecodeError, UnicodeDecodeError):
        _quarantine_corrupt_store(store_file)
        return {}, "corrupt_store"
    if not isinstance(data, dict) or any(not isinstance(value, dict) for value in data.values()):
        _quarantine_corrupt_store(store_file)
        return {}, "corrupt_store"
    return data, "ok"


def _read_all(store_file: str | os.PathLike[str]) -> dict[str, dict[str, Any]]:
    return _read_all_with_status(store_file)[0]


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
    if not rec or rec.get("schema_version") == V4_SCHEMA_VERSION:
        return None
    return PrewalkState.from_dict(rec)


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
# V4 durable state. The 0.3 adapter state above remains available only while
# the host integrations move to the ADR 0001 protocol issue by issue.
# ---------------------------------------------------------------------------

V4_SCHEMA_VERSION = 4
V4_PLANNING = "planning"
V4_CHECKPOINT_READY = "checkpoint_ready"
V4_HANDOFF_REQUESTED = "handoff_requested"
V4_EXECUTOR_RUNNING = "executor_running"
V4_INCOMPLETE = "incomplete"
V4_STALE = "stale"
V4_DEFAULT_STALE_SECONDS = 24 * 60 * 60
V4_PHASES = {
    V4_PLANNING,
    V4_CHECKPOINT_READY,
    V4_HANDOFF_REQUESTED,
    V4_EXECUTOR_RUNNING,
    V4_INCOMPLETE,
    V4_STALE,
}
V4_CHECKPOINT_PHASES = V4_PHASES - {V4_PLANNING}
V4_ROUTE_PHASES = {
    V4_HANDOFF_REQUESTED,
    V4_EXECUTOR_RUNNING,
    V4_INCOMPLETE,
    V4_STALE,
}
V4_PACKET_HEADINGS = (
    "Goal",
    "Files Read",
    "Constraints And Existing Patterns",
    "Full Todo List",
    "Task 1 Changes",
    "Verification Already Run",
    "Remaining Work",
    "Risks / Do Not Repeat",
)
class V4StateError(ValueError):
    """A v4 record or transition violates the durable workflow contract."""


@dataclass
class V4State:
    root_session_id: str
    workspace_id: str
    host: str
    schema_version: int = V4_SCHEMA_VERSION
    phase: str = V4_PLANNING
    preset: str = DEFAULT_PRESET
    executor_model: str = ""
    executor_effort: str = ""
    max_todos: int = DEFAULT_MAX_TODOS
    handoff_mode: str = "auto"
    require_model_routing: bool = True
    fast_mode: bool = False
    model_routing_proven: bool = False
    effort_routing_proven: bool = False
    todos: list[Todo] = field(default_factory=list)
    packet: str = ""
    verification_evidence: list[str] = field(default_factory=list)
    verification_warning: str = ""
    route_token: str = ""
    route_task_name: str = ""
    route_tool_use_id: str = ""
    executor_agent_id: str = ""
    route_attempt: int = 0
    launch_acknowledged: bool = False
    created_at: str = ""
    updated_at: str = ""
    checkpoint_at: str = ""
    route_requested_at: str = ""
    executor_started_at: str = ""
    last_event_at: str = ""
    revision: int = 0
    processed_event_ids: list[str] = field(default_factory=list)
    last_error: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> "V4State":
        if not isinstance(raw, dict):
            raise V4StateError("v4 state record must be an object")
        known = {name for name in cls.__dataclass_fields__}  # type: ignore[attr-defined]
        values = {key: value for key, value in raw.items() if key in known}
        todos = values.get("todos", [])
        if not isinstance(todos, list):
            raise V4StateError("v4 todo snapshot must be a list")
        values["todos"] = [
            todo if isinstance(todo, Todo) else Todo(**todo)
            for todo in todos
            if isinstance(todo, (Todo, dict))
        ]
        if len(values["todos"]) != len(todos):
            raise V4StateError("v4 todo snapshot contains an invalid item")
        try:
            return cls(**values)
        except TypeError as exc:
            raise V4StateError(f"v4 state record is partial: {exc}") from exc


@dataclass(frozen=True)
class V4LoadResult:
    state: V4State | None
    status: str
    message: str = ""
    next_command: str = ""


def utc_timestamp() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def _parse_v4_timestamp(value: Any, field_name: str) -> datetime:
    if not isinstance(value, str) or not value:
        raise V4StateError(f"{field_name} must be an ISO-8601 timestamp")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise V4StateError(f"{field_name} must be an ISO-8601 timestamp") from exc
    if parsed.tzinfo is None:
        raise V4StateError(f"{field_name} must include a timezone")
    return parsed


def workspace_identity(workspace_root: str | os.PathLike[str]) -> str:
    """Return a stable, non-reversible identity for a canonical workspace path."""
    canonical = str(Path(workspace_root).expanduser().resolve())
    return "ws-" + hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:24]


def _missing_packet_headings(packet: str) -> list[str]:
    missing: list[str] = []
    for heading in V4_PACKET_HEADINGS:
        pattern = rf"(?mi)^\s*(?:#{{1,6}}\s*)?{re.escape(heading)}\s*:?(?:\s+.*)?$"
        if not re.search(pattern, packet or ""):
            missing.append(heading)
    return missing


def validate_v4_state(state: V4State) -> None:
    """Raise :class:`V4StateError` unless every phase invariant holds."""
    if state.schema_version != V4_SCHEMA_VERSION:
        raise V4StateError(f"unsupported state schema {state.schema_version!r}")
    if not isinstance(state.root_session_id, str) or not state.root_session_id.strip():
        raise V4StateError("root_session_id is required")
    if not isinstance(state.workspace_id, str) or not state.workspace_id.strip():
        raise V4StateError("workspace_id is required")
    if state.host not in ("codex", "claude"):
        raise V4StateError("host must be codex or claude")
    if state.phase not in V4_PHASES:
        raise V4StateError(f"invalid v4 phase {state.phase!r}")
    string_fields = (
        "preset",
        "executor_model",
        "executor_effort",
        "handoff_mode",
        "packet",
        "verification_warning",
        "route_token",
        "route_task_name",
        "route_tool_use_id",
        "executor_agent_id",
        "last_error",
    )
    if any(not isinstance(getattr(state, name), str) for name in string_fields):
        raise V4StateError("v4 text fields must be strings")
    if not isinstance(state.max_todos, int) or isinstance(state.max_todos, bool) or state.max_todos < 1:
        raise V4StateError("max_todos must be positive")
    if state.handoff_mode not in HANDOFF_MODES:
        raise V4StateError(f"invalid handoff mode {state.handoff_mode!r}")
    created = _parse_v4_timestamp(state.created_at, "created_at")
    updated = _parse_v4_timestamp(state.updated_at, "updated_at")
    if updated < created:
        raise V4StateError("updated_at cannot precede created_at")
    if not state.last_event_at:
        raise V4StateError("last_event_at is required")
    for field_name in (
        "checkpoint_at",
        "route_requested_at",
        "executor_started_at",
        "last_event_at",
    ):
        value = getattr(state, field_name)
        if value:
            parsed = _parse_v4_timestamp(value, field_name)
            if parsed < created or parsed > updated:
                raise V4StateError(f"{field_name} must fall between created_at and updated_at")
    if (
        not isinstance(state.revision, int)
        or isinstance(state.revision, bool)
        or not isinstance(state.route_attempt, int)
        or isinstance(state.route_attempt, bool)
        or state.revision < 0
        or state.route_attempt < 0
    ):
        raise V4StateError("revision and route_attempt cannot be negative")
    if (
        not isinstance(state.require_model_routing, bool)
        or not isinstance(state.fast_mode, bool)
        or not isinstance(state.model_routing_proven, bool)
        or not isinstance(state.effort_routing_proven, bool)
        or not isinstance(state.launch_acknowledged, bool)
    ):
        raise V4StateError("v4 capability flags must be booleans")
    if not isinstance(state.processed_event_ids, list) or not all(
        isinstance(event_id, str) and event_id for event_id in state.processed_event_ids
    ):
        raise V4StateError("processed event IDs must be non-empty strings")
    if len(set(state.processed_event_ids)) != len(state.processed_event_ids):
        raise V4StateError("processed event IDs must be unique")

    if not isinstance(state.todos, list) or not all(isinstance(todo, Todo) for todo in state.todos):
        raise V4StateError("v4 todo snapshot must contain normalized Todo records")
    if any(
        not isinstance(value, str)
        for todo in state.todos
        for value in (todo.id, todo.content, todo.status)
    ):
        raise V4StateError("v4 todo fields must be strings")
    if not isinstance(state.verification_evidence, list) or not all(
        isinstance(item, str) and item.strip() for item in state.verification_evidence
    ):
        raise V4StateError("verification evidence must contain non-empty strings")
    if state.todos:
        error = validate_todo_list(state.todos, state.max_todos)
        if error:
            raise V4StateError(error)
        if any(todo.is_pause for todo in state.todos):
            raise V4StateError("v4 todos contain real work only; PAUSE is not a work item")
        ids = [todo.id.strip() for todo in state.todos]
        if len(set(ids)) != len(ids):
            raise V4StateError("v4 todo IDs must be unique")

    if state.phase in V4_CHECKPOINT_PHASES:
        if not state.todos:
            raise V4StateError("checkpoint phases require a durable todo snapshot")
        if state.todos[0].status != "completed":
            raise V4StateError("checkpoint task 1 must be completed")
        if count_remaining(state.todos) < 2:
            raise V4StateError("checkpoint requires at least two remaining real tasks")
        missing = _missing_packet_headings(state.packet)
        if missing:
            raise V4StateError("checkpoint packet is missing headings: " + ", ".join(missing))
        if not state.verification_evidence and not state.verification_warning.strip():
            raise V4StateError("checkpoint requires verification evidence or an explicit warning")
        if not state.checkpoint_at:
            raise V4StateError("checkpoint_at is required after checkpoint capture")

    if state.phase in V4_ROUTE_PHASES:
        if not state.route_token or not state.route_task_name:
            raise V4StateError("route phases require a token and task name")
        if state.route_attempt < 1 or not state.route_requested_at:
            raise V4StateError("route phases require an attempt and request timestamp")

    if state.phase == V4_EXECUTOR_RUNNING:
        if not state.executor_agent_id:
            raise V4StateError("executor_running requires a bound agent ID")
        if not state.executor_started_at:
            raise V4StateError("executor_running requires executor_started_at")
    if state.phase in (V4_INCOMPLETE, V4_STALE) and not state.last_error.strip():
        raise V4StateError(f"{state.phase} requires a recovery error")


def new_v4_state(
    root_session_id: str,
    workspace_id: str,
    host: str,
    *,
    now: str | None = None,
    **settings: Any,
) -> V4State:
    timestamp = now or utc_timestamp()
    state = V4State(
        root_session_id=root_session_id.strip(),
        workspace_id=workspace_id.strip(),
        host=host,
        created_at=timestamp,
        updated_at=timestamp,
        last_event_at=timestamp,
        **settings,
    )
    validate_v4_state(state)
    return state


def create_v4_state(store_file: str | os.PathLike[str], state: V4State) -> None:
    """Atomically create one root record without replacing an existing run."""
    validate_v4_state(state)
    with _store_lock(store_file):
        data = _read_all(store_file)
        if state.root_session_id in data:
            raise V4StateError("a state record already exists for this root session")
        data[state.root_session_id] = state.to_dict()
        _write_all(store_file, data)


def start_v4_run(
    store_file: str | os.PathLike[str],
    root_session_id: str,
    workspace_root: str | os.PathLike[str],
    host: str,
    preset: "Preset",
    *,
    fast_mode: bool = False,
    now: str | None = None,
) -> V4State:
    """Arm a schema-4 run, replacing only this root session's prior record."""
    state = new_v4_state(
        root_session_id,
        workspace_identity(workspace_root),
        host,
        now=now,
        preset=preset.name,
        executor_model=preset.executor_model,
        executor_effort=preset.executor_effort,
        max_todos=preset.max_todos,
        handoff_mode=preset.handoff_mode,
        require_model_routing=preset.require_model_routing,
        fast_mode=fast_mode,
    )
    with _store_lock(store_file):
        data, status = _read_all_with_status(store_file)
        if status == "corrupt_store":
            data = {}
        data[root_session_id] = state.to_dict()
        _write_all(store_file, data)
    return state


def load_v4_state(
    store_file: str | os.PathLike[str],
    root_session_id: str,
    *,
    workspace_id: str = "",
) -> V4LoadResult:
    """Load one v4 record and report deterministic reset/recovery information."""
    root_session_id = (root_session_id or "").strip()
    if not root_session_id:
        return V4LoadResult(
            None,
            "missing_identity",
            "Prewalk cannot resolve the active root session identity.",
            "pw-doctor",
        )
    with _store_lock(store_file):
        data, store_status = _read_all_with_status(store_file)
        if store_status == "corrupt_store":
            return V4LoadResult(
                None,
                "corrupt_store",
                "Prewalk quarantined an unreadable state store. Re-arm this run.",
                "prewalk",
            )
        raw = data.get(root_session_id)
        if raw is None:
            return V4LoadResult(None, "missing")
        record_version = raw.get("schema_version")
        if record_version in (None, 3):
            data.pop(root_session_id, None)
            _write_all(store_file, data)
            return V4LoadResult(
                None,
                "legacy_reset",
                "Prewalk reset an incompatible 0.3.x run; worktree files and host todos were unchanged.",
                "prewalk",
            )
        if record_version != V4_SCHEMA_VERSION:
            return V4LoadResult(
                None,
                "unsupported_version",
                f"Prewalk state schema {record_version!r} is not supported by this plugin and was not changed.",
                "pw-doctor",
            )
        try:
            state = V4State.from_dict(raw)
            validate_v4_state(state)
        except (AttributeError, TypeError, V4StateError, ValueError) as exc:
            return V4LoadResult(
                None,
                "invalid",
                f"Prewalk found an invalid v4 state record: {exc}",
                "pw-off",
            )
        if state.root_session_id != root_session_id:
            return V4LoadResult(
                None,
                "invalid",
                "Prewalk state key conflicts with its root session identity.",
                "pw-off",
            )
        if workspace_id and state.workspace_id != workspace_id:
            return V4LoadResult(
                None,
                "workspace_mismatch",
                "Prewalk state belongs to a different workspace and was not changed.",
                "pw-doctor",
            )
        if state.phase == V4_STALE:
            return V4LoadResult(
                state,
                "stale",
                "Prewalk cannot prove whether the bound executor is still running.",
                "pw-reconcile",
            )
        if state.phase == V4_INCOMPLETE:
            return V4LoadResult(
                state,
                "incomplete",
                "Prewalk retained the durable checkpoint after an incomplete executor attempt.",
                "pw-retry",
            )
        return V4LoadResult(state, "ok")


_V4_IMMUTABLE_FIELDS = {
    "schema_version",
    "root_session_id",
    "workspace_id",
    "host",
    "created_at",
    "revision",
    "processed_event_ids",
}


def apply_v4_transition(
    store_file: str | os.PathLike[str],
    root_session_id: str,
    *,
    expected_phases: Iterable[str],
    target_phase: str,
    event_id: str,
    updates: dict[str, Any] | None = None,
    now: str | None = None,
) -> V4State:
    """Apply one locked, invariant-checked, event-idempotent v4 transition."""
    expected = set(expected_phases)
    if not event_id.strip():
        raise V4StateError("transition event_id is required")
    if target_phase not in V4_PHASES:
        raise V4StateError(f"invalid target phase {target_phase!r}")
    changes = dict(updates or {})
    forbidden = _V4_IMMUTABLE_FIELDS.intersection(changes)
    if forbidden:
        raise V4StateError("transition cannot replace immutable fields: " + ", ".join(sorted(forbidden)))
    unknown = set(changes) - set(V4State.__dataclass_fields__)  # type: ignore[attr-defined]
    if unknown:
        raise V4StateError("transition contains unknown fields: " + ", ".join(sorted(unknown)))

    with _store_lock(store_file):
        data, status = _read_all_with_status(store_file)
        if status == "corrupt_store":
            raise V4StateError("state store was corrupt and has been quarantined")
        raw = data.get(root_session_id)
        if raw is None or raw.get("schema_version") != V4_SCHEMA_VERSION:
            raise V4StateError("no v4 state exists for this root session")
        state = V4State.from_dict(raw)
        validate_v4_state(state)
        if event_id in state.processed_event_ids:
            return state
        if state.phase not in expected:
            raise V4StateError(
                f"event {event_id!r} cannot transition {state.phase!r}; expected {sorted(expected)!r}"
            )
        for key, value in changes.items():
            if key == "todos":
                value = [todo if isinstance(todo, Todo) else Todo(**todo) for todo in value]
            setattr(state, key, value)
        timestamp = now or utc_timestamp()
        if _parse_v4_timestamp(timestamp, "updated_at") < _parse_v4_timestamp(
            state.updated_at, "previous updated_at"
        ):
            raise V4StateError("transition timestamp cannot precede the current state")
        state.phase = target_phase
        state.updated_at = timestamp
        state.last_event_at = timestamp
        state.revision += 1
        state.processed_event_ids = state.processed_event_ids + [event_id]
        validate_v4_state(state)
        data[root_session_id] = state.to_dict()
        _write_all(store_file, data)
        return state


@dataclass(frozen=True)
class V4CheckpointResult:
    status: str
    message: str
    state: V4State | None = None


def _v4_content_event_id(kind: str, root_session_id: str, *values: Any) -> str:
    encoded = json.dumps(values, ensure_ascii=False, sort_keys=True, default=str)
    digest = hashlib.sha256(encoded.encode("utf-8")).hexdigest()[:24]
    return f"{kind}:{root_session_id}:{digest}"


def record_v4_todos(
    store_file: str | os.PathLike[str],
    root_session_id: str,
    todos: list[Todo],
    *,
    event_id: str = "",
) -> V4CheckpointResult:
    """Persist a complete real-work snapshot without creating a checkpoint."""
    loaded = load_v4_state(store_file, root_session_id)
    if loaded.state is None or loaded.state.phase != V4_PLANNING:
        return V4CheckpointResult(loaded.status, loaded.message, loaded.state)
    if any(todo.is_pause for todo in todos):
        return V4CheckpointResult(
            "invalid_todos", "Prewalk v4 plans contain real work only; remove the PAUSE todo."
        )
    error = validate_todo_list(todos, loaded.state.max_todos)
    if error:
        return V4CheckpointResult("invalid_todos", error)
    transition_id = event_id.strip() or _v4_content_event_id(
        "todos", root_session_id, [asdict(todo) for todo in todos]
    )
    state = apply_v4_transition(
        store_file,
        root_session_id,
        expected_phases=[V4_PLANNING],
        target_phase=V4_PLANNING,
        event_id=transition_id,
        updates={"todos": todos},
    )
    return V4CheckpointResult("recorded", "Prewalk recorded the real todo snapshot.", state)


def _packet_section(packet: str, heading: str) -> str:
    heading_names = "|".join(re.escape(item) for item in V4_PACKET_HEADINGS)
    match = re.search(
        rf"(?mis)^\s*(?:#{{1,6}}\s*)?{re.escape(heading)}\s*:?\s*(.*?)"
        rf"(?=^\s*(?:#{{1,6}}\s*)?(?:{heading_names})\s*:?(?:\s|$)|\Z)",
        packet,
    )
    return match.group(1).strip() if match else ""


_VERIFICATION_WARNING_RE = re.compile(
    r"\b(?:warning|not run|not available|unavailable|unable to|could not|no (?:test|check|verification))\b",
    re.I,
)


def packet_verification(packet: str) -> tuple[list[str], str]:
    """Return exact verification evidence, or an explicit warning, from a packet."""
    section = _packet_section(packet, "Verification Already Run")
    if not section:
        return [], ""
    if _VERIFICATION_WARNING_RE.search(section):
        return [], section
    evidence = [line.strip(" -*\t") for line in section.splitlines() if line.strip(" -*\t")]
    return evidence, ""


def capture_v4_checkpoint(
    store_file: str | os.PathLike[str],
    root_session_id: str,
    *,
    packet: str,
    todos: list[Todo] | None = None,
    event_id: str = "",
    now: str | None = None,
) -> V4CheckpointResult:
    """Validate one root Stop event and durably capture its exact assistant packet."""
    loaded = load_v4_state(store_file, root_session_id)
    state = loaded.state
    if state is None:
        return V4CheckpointResult(loaded.status, loaded.message)
    if state.phase == V4_CHECKPOINT_READY:
        return V4CheckpointResult("checkpoint_ready", "Prewalk checkpoint is already ready.", state)
    if state.phase != V4_PLANNING:
        return V4CheckpointResult(
            "not_planning", f"Prewalk cannot capture a checkpoint from {state.phase}.", state
        )

    snapshot = list(todos) if todos else list(state.todos)
    if not snapshot:
        if packet.strip() and len(_missing_packet_headings(packet)) < len(V4_PACKET_HEADINGS):
            return V4CheckpointResult(
                "missing_todos",
                "Prewalk cannot capture a handoff packet without a complete real todo snapshot.",
            )
        clear_state(store_file, root_session_id)
        return V4CheckpointResult(
            "trivial", "prewalk: trivial task; no handoff checkpoint was created."
        )
    if any(todo.is_pause for todo in snapshot):
        return V4CheckpointResult(
            "invalid_todos", "Prewalk v4 plans contain real work only; remove the PAUSE todo."
        )
    error = validate_todo_list(snapshot, state.max_todos)
    if error:
        return V4CheckpointResult("invalid_todos", error)
    if snapshot[0].status != "completed":
        return V4CheckpointResult(
            "incomplete_task_one", "Prewalk task #1 must be completed before checkpoint capture."
        )
    remaining = count_remaining(snapshot)
    if remaining == 0:
        clear_state(store_file, root_session_id)
        return V4CheckpointResult("complete", NO_HANDOFF_NEEDED)
    if remaining == 1:
        clear_state(store_file, root_session_id)
        return V4CheckpointResult("one_remaining", ONE_LEFT_HINT)

    missing = _missing_packet_headings(packet)
    if missing:
        return V4CheckpointResult(
            "invalid_packet", "Prewalk checkpoint packet is missing headings: " + ", ".join(missing)
        )
    evidence, warning = packet_verification(packet)
    if not evidence and not warning:
        return V4CheckpointResult(
            "missing_evidence",
            "Prewalk checkpoint requires verification evidence or an explicit verification warning.",
        )
    timestamp = now or utc_timestamp()
    transition_id = event_id.strip() or _v4_content_event_id(
        "root-stop", root_session_id, [asdict(todo) for todo in snapshot], packet
    )
    try:
        checkpoint = apply_v4_transition(
            store_file,
            root_session_id,
            expected_phases=[V4_PLANNING],
            target_phase=V4_CHECKPOINT_READY,
            event_id=transition_id,
            now=timestamp,
            updates={
                "todos": snapshot,
                "packet": packet,
                "verification_evidence": evidence,
                "verification_warning": warning,
                "checkpoint_at": timestamp,
            },
        )
    except V4StateError as exc:
        return V4CheckpointResult("invalid_checkpoint", str(exc))
    return V4CheckpointResult(
        "checkpoint_ready",
        "prewalk: checkpoint ready; run `pw-go` to hand off or `pw-revise` to revise.",
        checkpoint,
    )


def v4_handoff_context(
    store_file: str | os.PathLike[str], root_session_id: str
) -> V4CheckpointResult:
    """Load the durable packet used by host-specific routing after resume."""
    loaded = load_v4_state(store_file, root_session_id)
    state = loaded.state
    if state is None:
        return V4CheckpointResult(loaded.status, loaded.message)
    if state.phase != V4_CHECKPOINT_READY:
        return V4CheckpointResult(
            "not_ready", f"Prewalk has no checkpoint ready for handoff ({state.phase}).", state
        )
    return V4CheckpointResult(
        "checkpoint_ready", f"{HANDOFF_NOTE}\n\n{state.packet}", state
    )


def revise_v4_checkpoint(
    store_file: str | os.PathLike[str], root_session_id: str, revision: str
) -> V4CheckpointResult:
    """Return a durable checkpoint to planning so root Stop can replace it."""
    loaded = load_v4_state(store_file, root_session_id)
    state = loaded.state
    if state is None:
        return V4CheckpointResult(loaded.status, loaded.message)
    if state.phase not in (V4_CHECKPOINT_READY, V4_INCOMPLETE):
        return V4CheckpointResult(
            "not_ready", f"Prewalk has no checkpoint ready to revise ({state.phase}).", state
        )
    event_id = _v4_content_event_id(
        "revise", root_session_id, state.revision, revision.strip()
    )
    revised = apply_v4_transition(
        store_file,
        root_session_id,
        expected_phases=[V4_CHECKPOINT_READY, V4_INCOMPLETE],
        target_phase=V4_PLANNING,
        event_id=event_id,
        updates={
            "packet": "",
            "verification_evidence": [],
            "verification_warning": "",
            "checkpoint_at": "",
            "route_token": "",
            "route_task_name": "",
            "route_tool_use_id": "",
            "executor_agent_id": "",
            "route_requested_at": "",
            "executor_started_at": "",
            "launch_acknowledged": False,
            "model_routing_proven": False,
            "effort_routing_proven": False,
            "last_error": "",
        },
    )
    instruction = (
        f"PREWALK REVISION: update the plan accordingly: {revision.strip() or '(no detail given)'}. "
        "Re-explore only what the revision affects, update only real work in the todo list, "
        "re-verify task #1 if it changed, then stop with a replacement structured Handoff Packet."
    )
    return V4CheckpointResult("planning", instruction, revised)


CODEX_EXECUTOR_INSTRUCTIONS = (
    "Continue only the remaining todos from the persisted packet. Do not repeat task #1 or restart "
    "planning. Mark one todo in progress at a time, run its stated verification, and finish with "
    "exactly PREWALK_COMPLETE when all work is verified, or PREWALK_INCOMPLETE: <reason>."
)

CLAUDE_EXECUTOR_AGENT = "prewalk:prewalk-executor"
CLAUDE_EXECUTOR_LIFECYCLE_TYPES = {CLAUDE_EXECUTOR_AGENT, "prewalk-executor"}
CLAUDE_EXECUTOR_INSTRUCTIONS = CODEX_EXECUTOR_INSTRUCTIONS


def claude_route_message(state: V4State) -> str:
    """Return the canonical fresh-context prompt installed by Claude's hook."""
    return (
        f"PREWALK_HANDOFF_TOKEN: {state.route_token}\n\n"
        f"{HANDOFF_NOTE}\n\n{state.packet}\n\n## Executor Contract\n"
        f"{CLAUDE_EXECUTOR_INSTRUCTIONS}"
    )


def claude_route_instruction(state: V4State) -> str:
    """Tell the root model how to make the one token-bearing Agent call."""
    return (
        "spawn ONE Task/Agent now. Use the complete text between "
        "PREWALK_MESSAGE_BEGIN and PREWALK_MESSAGE_END as its prompt. Do not set or "
        "change the subagent type or model; the PreToolUse hook owns both.\n"
        f"PREWALK_TASK_NAME: {state.route_task_name}\n"
        "PREWALK_MESSAGE_BEGIN\n"
        f"{claude_route_message(state)}\n"
        "PREWALK_MESSAGE_END"
    )


def request_claude_handoff(
    store_file: str | os.PathLike[str],
    root_session_id: str,
    *,
    environment: dict[str, str] | None = None,
) -> V4CheckpointResult:
    """Create one token-bound Claude route after proving hook model routing."""
    loaded = load_v4_state(store_file, root_session_id)
    state = loaded.state
    if state is None:
        return V4CheckpointResult(loaded.status, loaded.message)
    if state.phase == V4_HANDOFF_REQUESTED:
        return V4CheckpointResult(
            "handoff_requested", claude_route_instruction(state), state
        )
    if state.phase != V4_CHECKPOINT_READY:
        return V4CheckpointResult(
            "not_ready", f"Prewalk has no checkpoint ready for Claude routing ({state.phase}).", state
        )
    preset = Preset(
        state.preset,
        state.executor_model,
        executor_effort=state.executor_effort,
        require_model_routing=state.require_model_routing,
    )
    capability = evaluate_capabilities(
        preset, "claude", environment=environment or {}
    )
    if not capability.routing_allowed:
        return V4CheckpointResult(
            "unsupported_route",
            format_capability_report(capability)
            + "\nPrewalk retained the checkpoint; do not spawn an unpinned executor.",
            state,
        )

    token = secrets.token_urlsafe(24)
    attempt = state.route_attempt + 1
    timestamp = utc_timestamp()
    requested = apply_v4_transition(
        store_file,
        root_session_id,
        expected_phases=[V4_CHECKPOINT_READY],
        target_phase=V4_HANDOFF_REQUESTED,
        event_id=f"claude-route:{root_session_id}:{token}",
        now=timestamp,
        updates={
            "route_token": token,
            "route_task_name": f"prewalk_executor_{attempt}_{token[:8]}",
            "route_attempt": attempt,
            "route_requested_at": timestamp,
            "model_routing_proven": True,
            "effort_routing_proven": False,
            "route_tool_use_id": "",
            "executor_agent_id": "",
            "executor_started_at": "",
            "launch_acknowledged": False,
            "last_error": "",
        },
    )
    return V4CheckpointResult(
        "handoff_requested", claude_route_instruction(requested), requested
    )


def codex_route_message(state: V4State) -> str:
    """Return the one canonical fresh-context instruction for the Codex executor."""
    return (
        f"PREWALK_ROUTE_TOKEN: {state.route_token}\n\n"
        f"{HANDOFF_NOTE}\n\n{state.packet}\n\n## Executor Contract\n"
        f"{CODEX_EXECUTOR_INSTRUCTIONS}"
    )


def request_codex_handoff(
    store_file: str | os.PathLike[str],
    root_session_id: str,
    *,
    schema_fields: Iterable[str],
) -> V4CheckpointResult:
    """Create one token-bound Codex route after checking the live tool schema."""
    loaded = load_v4_state(store_file, root_session_id)
    state = loaded.state
    if state is None:
        return V4CheckpointResult(loaded.status, loaded.message)
    if state.phase == V4_HANDOFF_REQUESTED:
        return V4CheckpointResult(
            "handoff_requested", codex_route_message(state), state
        )
    if state.phase != V4_CHECKPOINT_READY:
        return V4CheckpointResult(
            "not_ready", f"Prewalk has no checkpoint ready for Codex routing ({state.phase}).", state
        )
    if state.handoff_mode == "manual-model":
        return V4CheckpointResult(
            "manual_required",
            "This preset requires an explicit manual model switch. The checkpoint remains durable; "
            "switch to the executor model and run pw-resume.",
            state,
        )
    fields = set(schema_fields)
    preset = Preset(
        state.preset,
        state.executor_model,
        executor_effort=state.executor_effort,
        require_model_routing=state.require_model_routing,
    )
    capability = evaluate_capabilities(preset, "codex", schema_fields=fields)
    if not capability.routing_allowed:
        return V4CheckpointResult(
            "unsupported_route",
            format_capability_report(capability)
            + "\nPrewalk retained the checkpoint; do not spawn an unpinned executor.",
            state,
        )
    required = {"task_name", "message", "fork_turns"}
    missing = sorted(required - fields)
    if missing:
        return V4CheckpointResult(
            "unsupported_route",
            "The live spawn_agent schema is missing required fields: " + ", ".join(missing),
            state,
        )

    token = secrets.token_urlsafe(24)
    attempt = state.route_attempt + 1
    task_name = f"prewalk_executor_{attempt}_{token[:8]}"
    timestamp = utc_timestamp()
    requested = apply_v4_transition(
        store_file,
        root_session_id,
        expected_phases=[V4_CHECKPOINT_READY],
        target_phase=V4_HANDOFF_REQUESTED,
        event_id=f"codex-route:{root_session_id}:{token}",
        now=timestamp,
        updates={
            "route_token": token,
            "route_task_name": task_name,
            "route_attempt": attempt,
            "route_requested_at": timestamp,
            "model_routing_proven": "model" in fields,
            "effort_routing_proven": bool(
                state.executor_effort and "reasoning_effort" in fields
            ),
            "route_tool_use_id": "",
            "executor_agent_id": "",
            "executor_started_at": "",
            "launch_acknowledged": False,
            "last_error": "",
        },
    )
    return V4CheckpointResult(
        "handoff_requested", codex_route_message(requested), requested
    )


def resume_codex_manual(
    store_file: str | os.PathLike[str], root_session_id: str
) -> V4CheckpointResult:
    """Explicit compatibility route after the user manually changes root model."""
    loaded = load_v4_state(store_file, root_session_id)
    state = loaded.state
    if state is None:
        return V4CheckpointResult(loaded.status, loaded.message)
    if state.phase != V4_CHECKPOINT_READY:
        return V4CheckpointResult(
            "not_ready", f"Prewalk has no checkpoint ready for manual resume ({state.phase}).", state
        )
    token = secrets.token_urlsafe(24)
    timestamp = utc_timestamp()
    running = apply_v4_transition(
        store_file,
        root_session_id,
        expected_phases=[V4_CHECKPOINT_READY],
        target_phase=V4_EXECUTOR_RUNNING,
        event_id=f"codex-manual-resume:{root_session_id}:{token}",
        now=timestamp,
        updates={
            "route_token": token,
            "route_task_name": f"manual_root_{token[:8]}",
            "route_attempt": state.route_attempt + 1,
            "route_requested_at": timestamp,
            "executor_agent_id": f"manual-root:{root_session_id}",
            "executor_started_at": timestamp,
            "launch_acknowledged": True,
            "last_error": "",
        },
    )
    return V4CheckpointResult(
        "executor_running",
        f"{HANDOFF_NOTE}\n\n{running.packet}\n\n## Executor Contract\n"
        f"{CODEX_EXECUTOR_INSTRUCTIONS}\n\n"
        "This is the explicit manual-root fallback. Run pw-complete or pw-incomplete when finished.",
        running,
    )


def finish_codex_manual(
    store_file: str | os.PathLike[str],
    root_session_id: str,
    *,
    complete: bool,
    detail: str = "",
) -> V4CheckpointResult:
    loaded = load_v4_state(store_file, root_session_id)
    state = loaded.state
    if (
        state is None
        or state.phase != V4_EXECUTOR_RUNNING
        or state.executor_agent_id != f"manual-root:{root_session_id}"
    ):
        return V4CheckpointResult(
            "not_manual", "No explicit manual-root Prewalk executor is active.", state
        )
    if complete:
        clear_state(store_file, root_session_id)
        return V4CheckpointResult("complete", "prewalk: manual executor completed all work.")
    reason = detail.strip() or "manual executor stopped with work remaining"
    incomplete = apply_v4_transition(
        store_file,
        root_session_id,
        expected_phases=[V4_EXECUTOR_RUNNING],
        target_phase=V4_INCOMPLETE,
        event_id=_v4_content_event_id("codex-manual-incomplete", root_session_id, reason),
        updates={"last_error": reason},
    )
    return V4CheckpointResult("incomplete", reason, incomplete)


@dataclass(frozen=True)
class V4RouteDecision:
    handled: bool
    allowed: bool
    message: str = ""
    state: V4State | None = None
    updated_input: dict[str, Any] | None = None


def _claude_agent_intended(state: V4State, tool_input: dict[str, Any]) -> bool:
    prompt = str(tool_input.get("prompt") or tool_input.get("description") or "")
    return bool(
        state.route_token
        and f"PREWALK_HANDOFF_TOKEN: {state.route_token}" in prompt.splitlines()
    )


def _fail_v4_route(
    store_file: str | os.PathLike[str],
    root_session_id: str,
    state: V4State,
    *,
    reason: str,
    event_id: str,
) -> V4RouteDecision:
    failed = apply_v4_transition(
        store_file,
        root_session_id,
        expected_phases=[state.phase],
        target_phase=V4_INCOMPLETE,
        event_id=event_id,
        updates={"last_error": reason.strip() or "executor route failed"},
    )
    return V4RouteDecision(True, False, failed.last_error, failed)


def validate_claude_agent_call(
    store_file: str | os.PathLike[str],
    root_session_id: str,
    tool_input: dict[str, Any],
    *,
    tool_use_id: str,
    environment: dict[str, str] | None = None,
) -> V4RouteDecision:
    """Rewrite only the token-bearing root Agent call and persist its tool ID."""
    loaded = load_v4_state(store_file, root_session_id)
    state = loaded.state
    if state is None:
        return V4RouteDecision(False, True, state=state)
    if state.phase != V4_HANDOFF_REQUESTED:
        if state.route_token and _claude_agent_intended(state, tool_input):
            return V4RouteDecision(
                True, False, f"Prewalk route is {state.phase}; do not reuse its token.", state
            )
        return V4RouteDecision(False, True, state=state)
    if not _claude_agent_intended(state, tool_input):
        return V4RouteDecision(False, True, state=state)
    if state.route_tool_use_id:
        if tool_use_id == state.route_tool_use_id:
            updated = dict(tool_input)
            updated.update(
                prompt=claude_route_message(state),
                subagent_type=CLAUDE_EXECUTOR_AGENT,
                model=state.executor_model,
            )
            return V4RouteDecision(
                True, True, "Prewalk Agent route was already accepted.", state, updated
            )
        return V4RouteDecision(
            True, False, "The pending Prewalk route is already claimed by another Agent call.", state
        )
    if not tool_use_id.strip():
        return _fail_v4_route(
            store_file,
            root_session_id,
            state,
            reason="Prewalk cannot safely route an Agent call without tool_use_id.",
            event_id=_v4_content_event_id("claude-agent-no-tool-id", root_session_id, tool_input),
        )
    preset = Preset(
        state.preset,
        state.executor_model,
        executor_effort=state.executor_effort,
        require_model_routing=state.require_model_routing,
    )
    capability = evaluate_capabilities(
        preset, "claude", environment=environment or {}
    )
    if not capability.routing_allowed:
        reason = format_capability_report(capability)
        return _fail_v4_route(
            store_file,
            root_session_id,
            state,
            reason=reason,
            event_id=f"claude-agent-route-conflict:{tool_use_id}",
        )

    updated = dict(tool_input)
    updated.update(
        prompt=claude_route_message(state),
        subagent_type=CLAUDE_EXECUTOR_AGENT,
        model=state.executor_model,
    )
    accepted = apply_v4_transition(
        store_file,
        root_session_id,
        expected_phases=[V4_HANDOFF_REQUESTED],
        target_phase=V4_HANDOFF_REQUESTED,
        event_id=f"claude-agent-pre:{tool_use_id}",
        updates={"route_tool_use_id": tool_use_id},
    )
    return V4RouteDecision(
        True, True, "Prewalk routed the exact token-bearing Agent call.", accepted, updated
    )


def bind_claude_executor(
    store_file: str | os.PathLike[str],
    root_session_id: str,
    *,
    agent_id: str,
    agent_type: str,
) -> V4RouteDecision:
    """Bind the first exact scoped SubagentStart after the accepted Agent call."""
    loaded = load_v4_state(store_file, root_session_id)
    state = loaded.state
    if state is None or agent_type not in CLAUDE_EXECUTOR_LIFECYCLE_TYPES:
        return V4RouteDecision(False, True, state=state)
    if state.phase == V4_EXECUTOR_RUNNING and agent_id == state.executor_agent_id:
        return V4RouteDecision(True, True, "Prewalk executor was already bound.", state)
    if state.phase != V4_HANDOFF_REQUESTED or not state.route_tool_use_id:
        return V4RouteDecision(False, True, state=state)
    if not agent_id.strip():
        return _fail_v4_route(
            store_file,
            root_session_id,
            state,
            reason="SubagentStart did not provide an executor agent identity.",
            event_id=f"claude-subagent-start-missing:{state.route_tool_use_id}",
        )
    timestamp = utc_timestamp()
    running = apply_v4_transition(
        store_file,
        root_session_id,
        expected_phases=[V4_HANDOFF_REQUESTED],
        target_phase=V4_EXECUTOR_RUNNING,
        event_id=f"claude-subagent-start:{state.route_tool_use_id}:{agent_id}",
        now=timestamp,
        updates={"executor_agent_id": agent_id, "executor_started_at": timestamp},
    )
    return V4RouteDecision(True, True, f"Prewalk bound executor {agent_id}.", running)


def acknowledge_claude_agent_call(
    store_file: str | os.PathLike[str],
    root_session_id: str,
    *,
    tool_use_id: str,
) -> V4RouteDecision:
    """Record Agent PostToolUse as launch acknowledgement, never completion."""
    loaded = load_v4_state(store_file, root_session_id)
    state = loaded.state
    if (
        state is None
        or state.phase not in (V4_HANDOFF_REQUESTED, V4_EXECUTOR_RUNNING)
        or not tool_use_id
        or tool_use_id != state.route_tool_use_id
    ):
        return V4RouteDecision(False, True, state=state)
    if state.launch_acknowledged:
        return V4RouteDecision(True, True, "Prewalk launch was already acknowledged.", state)
    acknowledged = apply_v4_transition(
        store_file,
        root_session_id,
        expected_phases=[state.phase],
        target_phase=state.phase,
        event_id=f"claude-agent-post:{tool_use_id}",
        updates={"launch_acknowledged": True},
    )
    return V4RouteDecision(
        True, True, "prewalk: Agent launch acknowledged; waiting for bound SubagentStop.", acknowledged
    )


def fail_claude_agent_call(
    store_file: str | os.PathLike[str],
    root_session_id: str,
    *,
    tool_use_id: str,
    reason: str,
) -> V4RouteDecision:
    """Retain a retryable checkpoint after exact Agent denial or launch failure."""
    loaded = load_v4_state(store_file, root_session_id)
    state = loaded.state
    if (
        state is None
        or state.phase not in (V4_HANDOFF_REQUESTED, V4_EXECUTOR_RUNNING)
        or not tool_use_id
        or tool_use_id != state.route_tool_use_id
    ):
        return V4RouteDecision(False, True, state=state)
    normalized = reason.strip() or "executor Agent failed or was rejected"
    return _fail_v4_route(
        store_file,
        root_session_id,
        state,
        reason=normalized,
        event_id=_v4_content_event_id("claude-agent-failed", root_session_id, tool_use_id, normalized),
    )


def _codex_spawn_intended(state: V4State, tool_input: dict[str, Any]) -> bool:
    task_name = str(tool_input.get("task_name") or "")
    message = str(tool_input.get("message") or "")
    return (
        task_name == state.route_task_name
        or state.route_token in task_name
        or state.route_token in message
    )


def validate_codex_spawn(
    store_file: str | os.PathLike[str],
    root_session_id: str,
    tool_input: dict[str, Any],
    *,
    tool_use_id: str,
) -> V4RouteDecision:
    """Validate and bind the exact pending Codex spawn request before execution."""
    loaded = load_v4_state(store_file, root_session_id)
    state = loaded.state
    if state is None:
        return V4RouteDecision(False, True, state=state)
    if state.phase != V4_HANDOFF_REQUESTED:
        if state.route_token and _codex_spawn_intended(state, tool_input):
            return V4RouteDecision(
                True,
                False,
                f"Prewalk route is {state.phase}; do not reuse its token or spawn request.",
                state,
            )
        return V4RouteDecision(False, True, state=state)
    if not _codex_spawn_intended(state, tool_input):
        return V4RouteDecision(False, True, state=state)

    errors: list[str] = []
    expected_message = codex_route_message(state)
    if str(tool_input.get("task_name") or "") != state.route_task_name:
        errors.append("task_name does not match the pending Prewalk route")
    if str(tool_input.get("message") or "") != expected_message:
        errors.append("message is not the exact persisted Prewalk packet")
    if tool_input.get("fork_turns") != "none":
        errors.append('fork_turns must be "none"')
    if state.model_routing_proven and tool_input.get("model") != state.executor_model:
        errors.append("model does not match the configured executor")
    if state.require_model_routing and not state.model_routing_proven:
        errors.append("required executor model routing was not proven")
    if state.effort_routing_proven:
        if tool_input.get("reasoning_effort") != state.executor_effort:
            errors.append("reasoning_effort does not match the configured executor effort")
    elif "reasoning_effort" in tool_input:
        errors.append("reasoning_effort was not exposed by the live schema")
    if not tool_use_id.strip():
        errors.append("spawn hook payload has no tool_use_id")

    if errors:
        timestamp = utc_timestamp()
        failed = apply_v4_transition(
            store_file,
            root_session_id,
            expected_phases=[V4_HANDOFF_REQUESTED],
            target_phase=V4_INCOMPLETE,
            event_id=_v4_content_event_id("codex-spawn-denied", root_session_id, errors, tool_input),
            now=timestamp,
            updates={"last_error": "; ".join(errors)},
        )
        return V4RouteDecision(True, False, failed.last_error, failed)

    accepted = apply_v4_transition(
        store_file,
        root_session_id,
        expected_phases=[V4_HANDOFF_REQUESTED],
        target_phase=V4_HANDOFF_REQUESTED,
        event_id=f"codex-spawn-pre:{tool_use_id}",
        updates={"route_tool_use_id": tool_use_id},
    )
    return V4RouteDecision(True, True, "Prewalk accepted the exact executor spawn.", accepted)


def bind_codex_executor(
    store_file: str | os.PathLike[str],
    root_session_id: str,
    *,
    tool_use_id: str,
    agent_id: str,
    success: bool,
    detail: str = "",
) -> V4RouteDecision:
    """Bind only the agent returned by the exact accepted spawn tool call."""
    loaded = load_v4_state(store_file, root_session_id)
    state = loaded.state
    if state is None:
        return V4RouteDecision(False, True, state=state)
    if state.phase == V4_EXECUTOR_RUNNING and (
        tool_use_id == state.route_tool_use_id and agent_id == state.executor_agent_id
    ):
        return V4RouteDecision(True, True, "Prewalk executor was already bound.", state)
    if state.phase != V4_HANDOFF_REQUESTED:
        return V4RouteDecision(False, True, state=state)
    if not tool_use_id or tool_use_id != state.route_tool_use_id:
        return V4RouteDecision(False, True, state=state)
    timestamp = utc_timestamp()
    if not success or not agent_id.strip():
        error = detail.strip() or "spawn_agent did not return a usable agent identity"
        failed = apply_v4_transition(
            store_file,
            root_session_id,
            expected_phases=[V4_HANDOFF_REQUESTED],
            target_phase=V4_INCOMPLETE,
            event_id=f"codex-spawn-post-failed:{tool_use_id}",
            now=timestamp,
            updates={"last_error": error},
        )
        return V4RouteDecision(True, False, error, failed)
    running = apply_v4_transition(
        store_file,
        root_session_id,
        expected_phases=[V4_HANDOFF_REQUESTED],
        target_phase=V4_EXECUTOR_RUNNING,
        event_id=f"codex-spawn-post:{tool_use_id}:{agent_id}",
        now=timestamp,
        updates={
            "executor_agent_id": agent_id,
            "executor_started_at": timestamp,
            "launch_acknowledged": True,
        },
    )
    return V4RouteDecision(True, True, f"Prewalk bound executor {agent_id}.", running)


def finish_v4_executor(
    store_file: str | os.PathLike[str],
    root_session_id: str,
    *,
    agent_id: str,
    result: str,
    event_id: str,
) -> V4RouteDecision:
    """Accept a final marker only from the executor identity bound to this root."""
    loaded = load_v4_state(store_file, root_session_id)
    state = loaded.state
    if state is None:
        return V4RouteDecision(False, True, state=state)
    if state.phase == V4_INCOMPLETE and agent_id == state.executor_agent_id:
        return V4RouteDecision(True, False, state.last_error, state)
    if state.phase != V4_EXECUTOR_RUNNING:
        return V4RouteDecision(False, True, state=state)
    if not agent_id or agent_id != state.executor_agent_id:
        return V4RouteDecision(False, True, state=state)
    final_line = next((line.strip() for line in reversed(result.splitlines()) if line.strip()), "")
    if final_line == "PREWALK_COMPLETE":
        clear_state(store_file, root_session_id)
        return V4RouteDecision(True, True, "prewalk: executor completed all work.")
    reason = (
        final_line.partition(":")[2].strip()
        if final_line.startswith("PREWALK_INCOMPLETE:")
        else "bound executor stopped without a valid final marker"
    )
    incomplete = apply_v4_transition(
        store_file,
        root_session_id,
        expected_phases=[V4_EXECUTOR_RUNNING],
        target_phase=V4_INCOMPLETE,
        event_id=event_id or _v4_content_event_id("executor-stop", root_session_id, agent_id, result),
        updates={"last_error": reason or "executor reported incomplete work"},
    )
    return V4RouteDecision(True, False, incomplete.last_error, incomplete)


def interrupt_v4_executor(
    store_file: str | os.PathLike[str],
    root_session_id: str,
    *,
    reason: str,
    event_id: str = "",
) -> V4RouteDecision:
    """Recover a bound route when the host explicitly reports root interruption."""
    loaded = load_v4_state(store_file, root_session_id)
    state = loaded.state
    if state is None or state.phase != V4_EXECUTOR_RUNNING:
        return V4RouteDecision(False, True, state=state)
    normalized = reason.strip()
    if not re.search(r"\b(?:interrupt(?:ed|ion)?|cancel(?:led|ed|ation)?|abort(?:ed)?)\b", normalized, re.I):
        return V4RouteDecision(False, True, state=state)
    incomplete = apply_v4_transition(
        store_file,
        root_session_id,
        expected_phases=[V4_EXECUTOR_RUNNING],
        target_phase=V4_INCOMPLETE,
        event_id=event_id or _v4_content_event_id("codex-interrupted", root_session_id, normalized),
        updates={"last_error": normalized or "executor was interrupted"},
    )
    return V4RouteDecision(True, False, incomplete.last_error, incomplete)


def detect_v4_stale(
    store_file: str | os.PathLike[str],
    root_session_id: str,
    *,
    now: str | None = None,
    timeout_seconds: int = V4_DEFAULT_STALE_SECONDS,
    workspace_id: str = "",
) -> V4CheckpointResult:
    """Mark an overdue ambiguous route stale without clearing or stopping it."""
    loaded = load_v4_state(store_file, root_session_id, workspace_id=workspace_id)
    state = loaded.state
    if state is None:
        return V4CheckpointResult(loaded.status, loaded.message)
    if state.phase == V4_STALE:
        return V4CheckpointResult("stale", state.last_error, state)
    if state.phase not in (V4_HANDOFF_REQUESTED, V4_EXECUTOR_RUNNING):
        return V4CheckpointResult(state.phase, "No active route can become stale.", state)
    if timeout_seconds < 1:
        return V4CheckpointResult(
            "invalid_timeout", "The stale timeout must be positive.", state
        )
    checked_at = now or utc_timestamp()
    checked = _parse_v4_timestamp(checked_at, "stale check time")
    reference_name = (
        "executor_started_at" if state.phase == V4_EXECUTOR_RUNNING else "route_requested_at"
    )
    reference_value = getattr(state, reference_name)
    reference = _parse_v4_timestamp(reference_value, reference_name)
    elapsed = (checked - reference).total_seconds()
    if elapsed < timeout_seconds:
        return V4CheckpointResult(
            "active", f"Route liveness is not stale ({int(max(elapsed, 0))}s elapsed).", state
        )
    reason = (
        f"No matching native lifecycle event was observed for {int(elapsed)}s; "
        "executor liveness is unknown and no agent was stopped or cleared."
    )
    stale = apply_v4_transition(
        store_file,
        root_session_id,
        expected_phases=[state.phase],
        target_phase=V4_STALE,
        event_id=_v4_content_event_id(
            "route-stale", root_session_id, state.route_attempt, reference_value, timeout_seconds
        ),
        now=checked_at,
        updates={"last_error": reason},
    )
    return V4CheckpointResult("stale", reason, stale)


def prepare_v4_retry(
    store_file: str | os.PathLike[str], root_session_id: str
) -> V4CheckpointResult:
    """Reset only a proven-incomplete route while retaining task 1 and its packet."""
    loaded = load_v4_state(store_file, root_session_id)
    state = loaded.state
    if state is None:
        return V4CheckpointResult(loaded.status, loaded.message)
    if state.phase == V4_CHECKPOINT_READY:
        return V4CheckpointResult("checkpoint_ready", "Prewalk retry is already prepared.", state)
    if state.phase == V4_HANDOFF_REQUESTED:
        return V4CheckpointResult(
            "handoff_requested",
            "A route is already pending; reuse it rather than creating another executor.",
            state,
        )
    if state.phase in (V4_EXECUTOR_RUNNING, V4_STALE):
        return V4CheckpointResult(
            "agent_may_be_running",
            "Prewalk will not retry while an executor may still be running; run pw-reconcile first.",
            state,
        )
    if state.phase != V4_INCOMPLETE:
        return V4CheckpointResult(
            "not_retryable", f"Prewalk cannot retry from {state.phase}.", state
        )
    prepared = apply_v4_transition(
        store_file,
        root_session_id,
        expected_phases=[V4_INCOMPLETE],
        target_phase=V4_CHECKPOINT_READY,
        event_id=f"route-retry:{root_session_id}:{state.route_attempt}:{state.revision}",
        updates={
            "route_token": "",
            "route_task_name": "",
            "route_tool_use_id": "",
            "executor_agent_id": "",
            "route_requested_at": "",
            "executor_started_at": "",
            "launch_acknowledged": False,
            "model_routing_proven": False,
            "effort_routing_proven": False,
            "last_error": "",
        },
    )
    return V4CheckpointResult(
        "checkpoint_ready",
        "Prewalk retained task 1 and the exact packet; request one new executor route.",
        prepared,
    )


def reconcile_v4_route(
    store_file: str | os.PathLike[str],
    root_session_id: str,
    *,
    confirmed_not_running: bool,
    detail: str = "",
) -> V4CheckpointResult:
    """Resolve an ambiguous route only after explicit external liveness proof."""
    loaded = load_v4_state(store_file, root_session_id)
    state = loaded.state
    if state is None:
        return V4CheckpointResult(loaded.status, loaded.message)
    if state.phase == V4_INCOMPLETE:
        return V4CheckpointResult(
            "incomplete", "Prewalk route is already reconciled; run pw-retry.", state
        )
    if state.phase not in (V4_HANDOFF_REQUESTED, V4_EXECUTOR_RUNNING, V4_STALE):
        return V4CheckpointResult(
            "not_ambiguous", f"Prewalk has no ambiguous route to reconcile ({state.phase}).", state
        )
    if not confirmed_not_running:
        return V4CheckpointResult(
            "confirmation_required",
            "Confirm through the native runtime or explicit user acknowledgement that the bound "
            "agent is not running. Prewalk did not change state or terminate an agent.",
            state,
        )
    reason = detail.strip() or "native runtime confirmed the prior executor is not running"
    incomplete = apply_v4_transition(
        store_file,
        root_session_id,
        expected_phases=[state.phase],
        target_phase=V4_INCOMPLETE,
        event_id=_v4_content_event_id(
            "route-reconciled", root_session_id, state.route_attempt, state.executor_agent_id, reason
        ),
        updates={"last_error": reason},
    )
    return V4CheckpointResult(
        "incomplete",
        "Prewalk retained the checkpoint and marked the prior route incomplete; run pw-retry.",
        incomplete,
    )


def _v4_command(host: str, name: str) -> str:
    return f"/prewalk:{name}" if host == "claude" else f"$prewalk:{name}"


def _short_status_text(value: str, limit: int = 180) -> str:
    compact = " ".join((value or "").split())
    return compact if len(compact) <= limit else compact[: limit - 3] + "..."


def format_v4_status(loaded: V4LoadResult, *, host: str = "codex") -> str:
    """Format one safe operational view without exposing packet or route token."""
    state = loaded.state
    if state is None:
        next_name = loaded.next_command or "prewalk"
        message = loaded.message or "No armed run exists for this root session."
        return (
            "prewalk v4: idle\n"
            f"  state: {loaded.status}; detail: {_short_status_text(message)}\n"
            f"  next: {_v4_command(host, next_name)}"
        )

    token_summary = (
        "sha256:" + hashlib.sha256(state.route_token.encode("utf-8")).hexdigest()[:10]
        if state.route_token else "none"
    )
    if state.verification_evidence:
        evidence = f"verified ({len(state.verification_evidence)} item(s))"
    elif state.verification_warning:
        evidence = "warning: " + _short_status_text(state.verification_warning)
    else:
        evidence = "not captured"
    remaining = [todo for todo in state.todos if todo.open]
    remaining_text = "; ".join(
        f"{todo.id or index + 1}:{_short_status_text(todo.content, 80)}"
        for index, todo in enumerate(remaining)
    ) or "none"
    next_name = {
        V4_PLANNING: "pw-status",
        V4_CHECKPOINT_READY: "pw-go",
        V4_HANDOFF_REQUESTED: "pw-go" if not state.route_tool_use_id else "pw-status",
        V4_EXECUTOR_RUNNING: "pw-status",
        V4_INCOMPLETE: "pw-retry",
        V4_STALE: "pw-reconcile",
    }[state.phase]
    actions = {
        V4_PLANNING: "continue task 1 and root Stop; disarm=pw-off",
        V4_CHECKPOINT_READY: "route=pw-go; revise=pw-revise; disarm=pw-off",
        V4_HANDOFF_REQUESTED: "reuse pending route; reconcile only after interruption; disarm=pw-off",
        V4_EXECUTOR_RUNNING: "wait; reconcile only after proving agent stopped; disarm does not stop it",
        V4_INCOMPLETE: "retry=pw-retry; revise=pw-revise; disarm=pw-off",
        V4_STALE: "reconcile=pw-reconcile after liveness proof; disarm does not stop it",
    }[state.phase]
    return (
        f"prewalk v4: {state.phase} [{state.preset}]\n"
        f"  host: {state.host}; workspace: {state.workspace_id}\n"
        f"  executor: model={state.executor_model or 'none'}; effort="
        f"{state.executor_effort or 'host-default'}; handoff={state.handoff_mode}; "
        f"routing_proven={'yes' if state.model_routing_proven else 'no'}\n"
        f"  evidence: {evidence}\n"
        f"  route: attempt={state.route_attempt}; token={token_summary}; task="
        f"{'set' if state.route_task_name else 'none'}; tool={state.route_tool_use_id or 'none'}; "
        f"launch_ack={'yes' if state.launch_acknowledged else 'no'}\n"
        f"  bound_agent: {state.executor_agent_id or 'none'}\n"
        f"  timestamps: created={state.created_at}; checkpoint={state.checkpoint_at or 'none'}; "
        f"route={state.route_requested_at or 'none'}; executor={state.executor_started_at or 'none'}; "
        f"last_event={state.last_event_at}\n"
        f"  remaining({len(remaining)}): {remaining_text}\n"
        f"  last_error: {_short_status_text(state.last_error) or 'none'}\n"
        f"  actions: {actions}\n"
        f"  next: {_v4_command(state.host, next_name)}"
    )


# ---------------------------------------------------------------------------
# Preset loading (tiny hand-rolled parsers — JSON for Claude Code, a minimal
# TOML subset for Codex; we avoid a PyYAML/tomli dependency.)
# ---------------------------------------------------------------------------

@dataclass
class Preset:
    name: str
    executor_model: str
    description: str = ""
    max_todos: int = DEFAULT_MAX_TODOS
    executor_effort: str = ""
    handoff_mode: str = "auto"
    require_model_routing: bool = True
    deprecation_warnings: list[str] = field(default_factory=list)

    @property
    def planner_model(self) -> str:
        """Compatibility view for the 0.3 adapter during the v4 rollout."""
        return "active-session"

    @property
    def planner_thinking(self) -> str:
        return ""

    @property
    def executor_thinking(self) -> str:
        return self.executor_effort


def _preset_warnings(raw: dict[str, Any]) -> list[str]:
    warnings: list[str] = []
    if "planner" in raw:
        warnings.append("planner is deprecated and ignored; the active root session is the planner")
    if "planner_thinking" in raw:
        warnings.append("planner_thinking is deprecated and ignored")
    if "executor_thinking" in raw and "executor_effort" not in raw:
        warnings.append("executor_thinking is deprecated; use executor_effort")
    return warnings


def load_presets_json(path: str | os.PathLike[str]) -> dict[str, Preset]:
    """Claude Code presets live in JSON. Schema:
    { "default": "code-value",
      "presets": { "<name>": { "executor": "...", "description": "...",
                               "max_todos": 12, "executor_effort": "..." }, ... } }

    Legacy planner fields are accepted only to produce deprecation warnings.
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
        executor = str(raw.get("executor") or "").strip()
        if not executor:
            continue
        out[name] = Preset(
            name=name,
            executor_model=executor,
            description=str(raw.get("description") or ""),
            max_todos=int(raw.get("max_todos") or DEFAULT_MAX_TODOS),
            executor_effort=str(
                raw.get("executor_effort") or raw.get("executor_thinking") or ""
            ).strip(),
            handoff_mode=_handoff_mode(raw.get("handoff_mode")),
            require_model_routing=bool(raw.get("require_model_routing", True)),
            deprecation_warnings=_preset_warnings(raw),
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
    executor = "gpt-5.6-terra"
    executor_effort = "medium"
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
    executor = str(bucket.get("executor") or "").strip()
    if not executor:
        return
    out[name] = Preset(
        name=name,
        executor_model=executor,
        description=str(bucket.get("description") or ""),
        max_todos=int(bucket.get("max_todos") or DEFAULT_MAX_TODOS),
        executor_effort=str(
            bucket.get("executor_effort") or bucket.get("executor_thinking") or ""
        ).strip(),
        handoff_mode=_handoff_mode(bucket.get("handoff_mode")),
        require_model_routing=bool(bucket.get("require_model_routing", True)),
        deprecation_warnings=_preset_warnings(bucket),
    )


@dataclass(frozen=True)
class CapabilityReport:
    host: str
    configured_model: str
    configured_effort: str
    model_requested: str
    model_proven: str
    effort_requested: str
    effort_proven: str
    routing_allowed: bool
    warnings: tuple[str, ...] = ()
    errors: tuple[str, ...] = ()


def evaluate_capabilities(
    preset: Preset,
    host: str,
    *,
    schema_fields: Iterable[str] | None = None,
    environment: dict[str, str] | None = None,
) -> CapabilityReport:
    """Separate configured controls from requested and runtime-proven controls."""
    fields = None if schema_fields is None else set(schema_fields)
    warnings = list(preset.deprecation_warnings)
    errors: list[str] = []
    configured_effort = preset.executor_effort or "host-default"

    if host == "codex":
        if fields is None:
            model_requested = "pending-live-schema"
            model_proven = "unproven"
            effort_requested = "pending-live-schema" if preset.executor_effort else "no"
            effort_proven = "unproven" if preset.executor_effort else "not-configured"
        else:
            model_requested = "yes" if "model" in fields else "no"
            model_proven = "supported" if "model" in fields else "unsupported"
            effort_requested = (
                "yes" if preset.executor_effort and "reasoning_effort" in fields else "no"
            )
            effort_proven = (
                "supported" if preset.executor_effort and "reasoning_effort" in fields
                else "unsupported" if preset.executor_effort
                else "not-configured"
            )
            if preset.require_model_routing and "model" not in fields:
                errors.append("live spawn_agent schema cannot prove configured model routing")
        return CapabilityReport(
            host=host,
            configured_model=preset.executor_model,
            configured_effort=configured_effort,
            model_requested=model_requested,
            model_proven=model_proven,
            effort_requested=effort_requested,
            effort_proven=effort_proven,
            routing_allowed=not errors,
            warnings=tuple(warnings),
            errors=tuple(errors),
        )

    if host != "claude":
        raise ValueError(f"unsupported host {host!r}")
    env = environment if environment is not None else os.environ
    override = env.get("CLAUDE_CODE_SUBAGENT_MODEL", "").strip()
    model_proven = "hook-rewrite"
    if override and override != preset.executor_model:
        detail = (
            f"CLAUDE_CODE_SUBAGENT_MODEL={override!r} conflicts with configured executor "
            f"{preset.executor_model!r}"
        )
        if preset.require_model_routing:
            errors.append(detail)
            model_proven = "override-conflict"
        else:
            warnings.append(detail)
            model_proven = "overridden"
    if preset.executor_effort:
        warnings.append("Claude does not expose a dynamic per-subagent effort control")
    return CapabilityReport(
        host=host,
        configured_model=preset.executor_model,
        configured_effort=configured_effort,
        model_requested="yes",
        model_proven=model_proven,
        effort_requested="no",
        effort_proven="unsupported" if preset.executor_effort else "not-configured",
        routing_allowed=not errors,
        warnings=tuple(warnings),
        errors=tuple(errors),
    )


def format_capability_report(report: CapabilityReport) -> str:
    lines = [
        f"  configured: executor={report.configured_model}; effort={report.configured_effort}",
        f"  requested : model={report.model_requested}; effort={report.effort_requested}",
        f"  proven    : model={report.model_proven}; effort={report.effort_proven}",
    ]
    lines.extend(f"  warning   : {warning}" for warning in report.warnings)
    lines.extend(f"  error     : {error}" for error in report.errors)
    return "\n".join(lines)


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
        "protocol entirely — complete the task directly, verify it, and stop without a todo list.\n"
        "1. EXPLORE the codebase deeply first: config files, entry points, every file relevant to the "
        "task; grep for existing patterns and conventions. Everything you read now is inherited by the "
        f"rest of the run — read what matters, once.\n"
        "2. When the approach is clear, create a todo list. Keep it tight (prefer at most "
        f"{max_todos} items). Each item must be a complete task: concrete file path + what to do + a "
        "verification criterion (include a word like verify/test/build/check). Item #1 must be the "
        "foundational task everything else builds on.\n"
        "3. Complete task #1 — and ONLY task #1. Make its edit(s), run its verification, and mark it "
        "completed only after the verification passes. Do not start #2.\n"
        "4. Leave only real work in the todo list, then STOP. End with a structured handoff packet "
        "using these exact headings: Goal, Files Read, Constraints "
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

    # Executor completion: all real todos done. The active root model never changed.
    if state.phase == EXECUTOR:
        if remaining == 0:
            clear_state(store_file, session_id)
            return HookAction(
                system_message="prewalk: all todos completed; the active root session was unchanged.",
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
        clear_state(store_file, session_id)
        return HookAction(
            system_message="prewalk: executor completed all work; the active root session was unchanged."
        )
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
    loaded = load_v4_state(store_file, session_id)
    host = loaded.state.host if loaded.state is not None else "codex"
    return format_v4_status(loaded, host=host)


def disarm(store_file: str | os.PathLike[str], session_id: str) -> str:
    if session_id not in _read_all(store_file):
        return "prewalk was not armed for this session."
    clear_state(store_file, session_id)
    return (
        "prewalk disarmed. State was cleared explicitly; no agent was stopped and "
        "the workspace, todos, and active root model were unchanged."
    )


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
