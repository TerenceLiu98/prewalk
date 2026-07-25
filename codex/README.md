# prewalk for Codex

> A frontier model explores + plans + lands the first verified edit, then hands
> off to a cheaper executor that finishes the rest.

A Codex **plugin** implementing the **prewalk** technique
([Can Bölük / Stencil](https://stencil.so/blog/prewalk)) for **Codex CLI**
([openai/codex](https://github.com/openai/codex)). Shares its engine with the
Claude Code version. Installed via the Codex **marketplace**.

## The handoff mechanism

Codex hooks/MCP tools **cannot** rewrite the next request (no updatedInput),
so Claude Code's automatic hook-routed Task rewrite cannot be ported. The
confirmed bridge: `/pw-go` injects a handoff note instructing the model to
call the native `spawn_agent` tool with `message=<summary>`,
`model=<executor>`, and `fork_context=false`. The executor starts on a fresh
context and receives the handoff summary as its instruction. An in-thread
`/model <executor>` switch is kept only as a fallback when subagents are
unavailable. The TOML agent file documents the policy but is not a named-agent
router in Codex.

## Install

**Prerequisite:** Python 3 on PATH (`python3 --version`).

Codex uses a **marketplace** model (there is no `codex plugin install <url>`):

```sh
codex plugin marketplace add TerenceLiu98/prewalk      # or a local path
codex plugin add prewalk@prewalk-marketplace
cp codex/presets.example.toml ~/.codex/prewalk-presets.toml   # then edit models
```
Restart Codex.

### Update / remove

```sh
codex plugin marketplace upgrade prewalk-marketplace   # re-pull after git changes
codex plugin remove prewalk
codex plugin marketplace remove prewalk-marketplace
```

> The plugin is self-contained: the engine is vendored at
> `hooks/_shared/prewalk_core.py` and `hooks/_bootstrap.py` finds it, and the
> skills reference their helper scripts by relative path (Codex runs a plugin
> skill with cwd = plugin root).

## Use

```
$prewalk Add a settings page with tabbed sections
... frontier explores, writes a capped todo, completes task #1, writes a handoff summary ...
/pw-go                  # spawn the executor subagent (cheap model)
/pw-revise <changes>    # revise the plan on the frontier instead
```

Status / disarm:
```sh
python3 <plugin>/hooks/_arm.py status "$CODEX_SESSION_ID"
python3 <plugin>/hooks/_arm.py disarm "$CODEX_SESSION_ID"
```

## How it works

```
$prewalk <task>           → arms the run (frontier model)
  frontier                → explores, writes a capped todo (every item has a
                            verify-word), completes ONLY task #1 + verifies it,
                            writes a handoff summary, stops
  (you review)
/pw-go                    → prints the handoff note; model calls native
                            spawn_agent with explicit executor model + summary
  executor (cheap model)  → finishes the remaining todos in order on a fresh
                            context, then reports completion
```

Hooks (`hooks/hooks.json`):
- **Stop** → `pause_detect.py`: detects the frontier's handoff point and the
  executor's completion.
- **PostToolUse** (`update_plan|todo`) → `todo_tracker.py`: tracks todo list
  progress and completion.
- **PostToolUse** (`apply_patch|Edit|Write`) → `edit_tracker.py`: observes the
  first successful edit to mark the run handoff-ready.

There is intentionally no `PreToolUse` hook: unlike Claude Code (whose
`PreToolUse` `handoff_router` rewrites the executor spawn via `updatedInput`),
Codex has no `updatedInput` capability, so there is nothing to intercept before
a tool runs. The edit observation lives on `PostToolUse`, where the tool's
success/failure result is available.

`$prewalk`, `/pw-go`, `/pw-revise` are skills that call the Python helpers in
`hooks/` (`_arm.py`, `_pw.py`). The hooks are invoked through
`scripts/prewalk_*.sh` wrappers (Codex runs a plugin hook with cwd = plugin root).

> **Codex hook coverage:** `PreToolUse`/`PostToolUse` only intercept "simple"
> shell calls and file-edit tools (`apply_patch`), not every command or `WebSearch`.

## Notes

- Edit the planner/executor models in `~/.codex/prewalk-presets.toml` to what
  your Codex install resolves (run `/model` in Codex to see ids).
- Per-session state lives in `~/.codex/prewalk-state.json`.

## Layout

```
codex/
├── .codex-plugin/plugin.json          # Codex plugin manifest
├── hooks.json                         # hook registration
├── scripts/
│   ├── prewalk_pause.sh               # Stop hook wrapper
│   ├── prewalk_edit_tracker.sh       # PostToolUse wrapper (edits)
│   └── prewalk_todo_tracker.sh        # PostToolUse wrapper (todos)
├── hooks/
│   ├── _bootstrap.py                  # locates prewalk_core.py from any layout
│   ├── _common.py                     # host I/O shim
│   ├── _arm.py                        # $prewalk helper: arm/status/disarm
│   ├── _pw.py                         # /pw-go + /pw-revise helper
│   ├── pause_detect.py                # Stop hook logic
│   ├── edit_tracker.py                # PostToolUse logic (edits)
│   ├── todo_tracker.py                # PostToolUse logic (todos)
│   └── _shared/prewalk_core.py
├── skills/{prewalk,pw-go,pw-revise}/SKILL.md
├── agents/prewalk-executor.toml       # executor policy/reference
└── presets.example.toml
```
(Plus `../.agents/plugins/marketplace.json` at the repo root — the marketplace
manifest that `codex plugin marketplace add` reads.)
