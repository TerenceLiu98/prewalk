# prewalk for Claude Code

> A frontier model explores + plans + lands the first verified edit, then hands
> off to a cheaper executor subagent that finishes the rest.

A Claude Code **plugin** implementing the **prewalk** technique
([Can Bölük / Stencil](https://stencil.so/blog/prewalk)). Shares its engine with
the Codex version (`../_shared/prewalk_core.py`).

## The handoff mechanism

Claude Code hooks **cannot** switch the running session's model — but a
`PreToolUse` hook **can** rewrite a subagent spawn's `model` and `subagent_type`
(via `hookSpecificOutput.updatedInput`). So prewalk hands off by spawning a
fresh `prewalk-executor` subagent with the executor model forced on, carrying the
frontier's handoff summary. (Subagent-routing mechanism after
[tzachbon/claude-model-router-hook](https://github.com/tzachbon/claude-model-router-hook).)

## Install

**Prerequisite:** Python 3 on PATH (`python3 --version`).

```sh
claude plugin marketplace add TerenceLiu98/prewalk      # or a local clone path
claude plugin install prewalk@prewalk
cp claude-code/presets.example.json ~/.claude/prewalk-presets.json   # then edit models
```
Restart Claude Code so the plugin loads.

### Update / remove

```sh
claude plugin marketplace update prewalk      # re-pull after git changes
claude plugin uninstall prewalk
claude plugin marketplace remove prewalk
```

> The plugin is self-contained: the engine is vendored at
> `hooks/_shared/prewalk_core.py`, `hooks/_bootstrap.py` finds it, and a
> `SessionStart` hook exposes the session id to skill commands via
> `CLAUDE_ENV_FILE` (Claude Code does not inject it into Bash-tool subprocesses).

## Use

```
/prewalk Add a settings page with tabbed sections
... frontier explores, writes a capped todo list, completes task #1, writes a handoff summary ...
/pw-go                  # spawn the executor (the hook routes it onto the executor model)
/pw-revise <changes>    # revise the plan on the frontier instead
```

Status / disarm (helpers, not skills):
```sh
python3 <plugin>/hooks/_arm.py status "$CLAUDE_SESSION_ID"
python3 <plugin>/hooks/_arm.py disarm "$CLAUDE_SESSION_ID"
```

## How it works

```
/prewalk <task>            → arms the run (frontier model)
  frontier                 → explores, writes a capped todo (every item has a
                             verify-word), completes ONLY task #1 + verifies it,
                             writes a handoff summary, stops
  (you review)
/pw-go                     → instructs the frontier to spawn ONE Task for the
                             remaining work
  handoff_router (PreToolUse on Task)
                           → rewrites that spawn into prewalk-executor with the
                             executor model forced on
  executor (cheap model)   → finishes the remaining todos in order, given the
                             handoff summary; reports when done
```

Hooks (`hooks/hooks.json`):
- **SessionStart** → `export_session_id.py`: exposes the session id to skill
  commands.
- **PostToolUse** (`TodoWrite|TaskCreate|TaskUpdate|TaskList`) → `todo_tracker.py`:
  tracks remaining tasks and detects completion.
- **PostToolUse** (`Write|Edit|MultiEdit`) → `edit_tracker.py`: on the frontier's
  first successful edit, arms the handoff.
- **PreToolUse** (`Task`) → `handoff_router.py`: rewrites the spawn onto the
  executor when the run is handoff-ready.

`/prewalk`, `/pw-go`, `/pw-revise` are skills that call the Python helpers in
`hooks/` (`_arm.py`, `_pw.py`).

## Notes

- The executor is a **subagent** — it gets the frontier's handoff *summary*, not
  the raw read trajectory. The frontier's job is to write a thorough handoff
  (files read, the plan, what #1 proved, what remains) so the executor needs no
  re-exploration.
- Edit the planner/executor models in `~/.claude/prewalk-presets.json` to what
  your install resolves (aliases `opus`/`sonnet`/`haiku`/`fable`, or full ids).
- Per-session state lives in `~/.claude/prewalk-state.json`.

## Layout

```
claude-code/
├── .claude-plugin/plugin.json          # Claude Code plugin manifest
├── hooks/
│   ├── hooks.json                      # hook registration
│   ├── _bootstrap.py                   # locates prewalk_core.py from any layout
│   ├── _common.py                      # host I/O shim + todo normalization
│   ├── _arm.py                         # /prewalk helper: arm/status/disarm
│   ├── _pw.py                          # /pw-go + /pw-revise helper
│   ├── export_session_id.py            # SessionStart: expose session id
│   ├── todo_tracker.py                 # PostToolUse: track tasks + completion
│   ├── edit_tracker.py                 # PostToolUse: arm handoff on first edit
│   ├── handoff_router.py               # PreToolUse: rewrite spawn onto executor
│   └── _shared/prewalk_core.py
├── skills/{prewalk,pw-go,pw-revise}/SKILL.md
├── agents/prewalk-executor.md          # the cheap-model executor subagent
├── settings.example.json               # loose-install fallback
└── presets.example.json
```
