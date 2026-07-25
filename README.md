# prewalk — for Codex and Claude Code

> A frontier model explores + plans + lands the **first verified edit**, then hands
> the work to a cheaper executor that finishes the rest. ~50% cost, ~95% of
> frontier quality.

Two implementations of the **prewalk** technique ([Can Bölük / Stencil — "You
only need the frontier model for one single edit"](https://stencil.so/blog/prewalk)),
one for **Codex CLI** and one for **Claude Code**. They share one engine and
differ only in a thin host adapter, the skills, and the config format.

```text
  ┌──────────────┐    explore deeply + write a capped todo list
  │  frontier    │    (every item has a verify-word) +
  │  (strong $)  │    complete ONLY task #1 and verify it
  └──────┬───────┘    → write a handoff summary → STOP
         │  /pw-go  →  spawn the executor
         ▼
  ┌──────────────┐    finish the remaining todos in order,
  │  executor    │    given the frontier's handoff summary
  │  (cheap $)   │    → report when done
  └──────────────┘
```

## Why it works

An agent's cost is in the **reads**, not the edits. Plan-then-execute makes a
cheap executor **re-read everything** to ground a plan *document*. Prewalk
instead has the frontier do the expensive exploration + plan + one verified
edit, then hands a **handoff summary** to a cheap executor that finishes by
imitation.

## How the handoff works (per host)

Neither host lets a hook switch the *running* session's model, so each host
hands off through the mechanism it does allow:

**Claude Code** — a `PreToolUse` hook rewrites the executor spawn so the
`prewalk-executor` subagent runs on the executor model. The executor is a fresh
subagent that inherits the frontier's handoff summary.

```text
   main session (opus)                         executor subagent (haiku)
  ┌─────────────────────┐                     ┌──────────────────────┐
  │ frontier: explore + │                     │                      │
  │ plan + task #1 edit │                     │                      │
  │ + handoff summary   │                     │                      │
  └─────────┬───────────┘                     │                      │
            │ /pw-go → spawn ONE Task          │                      │
            ▼                                 │                      │
  ┌─────────────────────┐  updatedInput:      │                      │
  │ PreToolUse hook     │  subagent=executor  │                      │
  │ (handoff_router)    │  model = haiku  ───▶│ finishes the rest,   │
  └─────────────────────┘  + handoff prompt   │ one todo at a time   │
                                              └──────────────────────┘
```

**Codex** — Codex has no `PreToolUse → updatedInput`, so a hook can't rewrite
the spawn. Instead `/pw-go` prints a handoff note instructing the model to call
the native `spawn_agent` tool with `message=<summary>`, `model=<executor>`, and
`fork_context=false`. The executor is a fresh-context subagent guided by the
handoff summary; the TOML file is policy/reference rather than a named-agent
router. (An in-thread `/model <executor>` switch remains available only as a
fallback when subagents are unavailable.)

```text
   frontier thread (opus)                     executor subagent (luna)
  ┌─────────────────────┐                     ┌──────────────────────┐
  │ frontier: explore + │                     │                      │
  │ plan + task #1 edit │                     │                      │
  │ + handoff summary   │                     │                      │
  └─────────┬───────────┘                     │                      │
            │ /pw-go → handoff note           │                      │
            ▼   "spawn_agent(prewalk-executor)"│                      │
  ┌─────────────────────┐  spawn_agent:       │                      │
  │ model runs the      │  agent=executor     │                      │
  │ spawn_agent call    │  (model pinned ────▶│ finishes the rest,   │
  │ itself              │   in the toml)      │ one todo at a time   │
  └─────────────────────┘  + handoff summary  └──────────────────────┘
```

So on **both** hosts the executor is a fresh-context subagent guided by a
handoff *summary*. The only difference is *who issues the spawn*: Claude Code's
hook rewrites it automatically; Codex's model issues `spawn_agent` itself from
the `/pw-go` handoff note.

## Install

### Claude Code (plugin)

```sh
claude plugin marketplace add TerenceLiu98/prewalk      # or a local path
claude plugin install prewalk@prewalk
cp claude-code/presets.example.json ~/.claude/prewalk-presets.json   # then edit models
```
Restart Claude Code, then `/prewalk <task>`.

### Codex (plugin, via marketplace)

```sh
codex plugin marketplace add TerenceLiu98/prewalk
codex plugin add prewalk@prewalk-marketplace
cp codex/presets.example.toml ~/.codex/prewalk-presets.toml   # then edit models
```

> Python 3 is the only prerequisite (`python3 --version`). No third-party deps.

## Use

```text
# Claude Code
/prewalk Add a settings page with tabbed sections
... frontier explores, plans, lands task #1, writes a handoff summary ...
/pw-go                       # spawn the executor (Claude Code) / switch model (Codex)

# Codex
$prewalk Add a settings page with tabbed sections
/pw-go
```

At the handoff point, review the plan and task #1, then run **`/pw-go`**. To
revise the plan on the frontier instead, run **`/pw-revise <changes>`**.

## Tech stack

- **Hooks/helpers**: Python 3, standard library only. Both hosts run hooks as
  `type:"command"` scripts, where Python's zero-compile story beats a runtime
  language.
- **Skills/agents**: Markdown + frontmatter (host-mandated).
- **Config**: JSON (Claude Code) / TOML (Codex).
- **Engine**: `_shared/prewalk_core.py` — state machine, todo validation, preset
  parsing, frontier/handoff prompts. Each plugin vendors a copy under
  `hooks/_shared/` and `hooks/_bootstrap.py` finds it from any install layout.

## Layout

```text
prewalk/
├── _shared/prewalk_core.py            # shared engine
├── .claude-plugin/marketplace.json    # Claude Code marketplace manifest
├── .agents/plugins/marketplace.json   # Codex marketplace manifest
├── claude-code/                       # Claude Code plugin
│   ├── .claude-plugin/plugin.json
│   ├── hooks/  hooks.json  _bootstrap _common _arm _pw
│   │           export_session_id  todo_tracker  edit_tracker  handoff_router  _shared/
│   ├── skills/{prewalk,pw-go,pw-revise}/SKILL.md
│   ├── agents/prewalk-executor.md
│   └── presets.example.json
├── codex/                             # Codex plugin
│   ├── .codex-plugin/plugin.json
│   ├── hooks/  hooks.json  _bootstrap _common _arm _pw
│   │           pause_detect  edit_tracker  todo_tracker  _shared/
│   ├── scripts/  prewalk_pause.sh  prewalk_edit_tracker.sh  prewalk_todo_tracker.sh
│   ├── skills/{prewalk,pw-go,pw-revise}/SKILL.md
│   ├── agents/prewalk-executor.toml
│   └── presets.example.toml
└── install.sh
```

## Attribution

Technique: Can Bölük / Stencil, ["You only need the frontier model for one single
edit"](https://stencil.so/blog/prewalk). Reference implementations:
[westfable/hermes-prewalk](https://github.com/ildunari/hermes-prewalk) (MIT),
[Daniel-97/opencode-prewalk](https://github.com/Daniel-97/opencode-prewalk) (MIT),
and the subagent-routing mechanism from
[tzachbon/claude-model-router-hook](https://github.com/tzachbon/claude-model-router-hook)
(MIT). This repo's engine + adapters are original, MIT-licensed.
