# prewalk — for Codex and Claude Code

> A frontier model explores + plans + lands the **first verified edit**, then hands
> the **live context window** to a cheaper executor that finishes the rest. ~50%
> cost, ~95% of frontier quality.

Two independent implementations of the **prewalk** technique ([Can Bölük / Stencil
— "You only need the frontier model for one single edit"](https://stencil.so/blog/prewalk)),
one for **Codex CLI** ([openai/codex](https://github.com/openai/codex)) and one for
**Claude Code** ([anthropics/claude-code](https://github.com/anthropics/claude-code)).
They share one engine and differ only in a thin host-I/O adapter, the skills, and
the config format.

```text
  ┌──────────────┐    explore deeply + write a capped todo
  │  frontier    │    (every item has a verify-word) +
  │  (strong $)  │    complete ONLY task #1 and verify it
  └──────┬───────┘    → add a `⏸️ PAUSE` todo → STOP
         │  /pw-go  →  /model <executor>   (switch, keep the whole trajectory)
         ▼
  ┌──────────────┐    finish the remaining todos in order,
  │  executor    │    inheriting the reads + todos + verified
  │  (cheap $)   │    edit #1 → /model <planner> restores at the end
  └──────────────┘
```

## Why it works

An agent's cost is in the **reads**, not the edits. Plan-then-execute makes a cheap
executor **re-read everything** to ground a plan *document*. Prewalk instead hands
over the **context window itself** — exploration done, todo list initialized, one
verified edit as an in-context example — then swaps to the cheap model, which
finishes by imitation.

## Install

### Codex (plugin)

Codex has no `codex plugin install <url>` — it uses a **marketplace** model.
This repo is already shaped as a marketplace:

```sh
codex plugin marketplace add TerenceLiu98/prewalk            # or a local path
codex plugin add prewalk@prewalk-marketplace
cp codex/presets.example.toml ~/.codex/prewalk-presets.toml   # then edit the models
```
See [`codex/README.md`](codex/README.md) for update/remove and details.

### Claude Code (skills + hooks)

```sh
./install.sh claude-code        # copies skills + presets, merges hooks into settings.json
# or manually — see claude-code/README.md
```

Then edit `~/.claude/prewalk-presets.json` (planner/executor models) and restart
Claude Code.

> Python 3 is the only prerequisite (`python3 --version`). No third-party deps.

## Use

```text
# Codex
$prewalk Add a settings page with tabbed sections
$prewalk Refactor the auth module --no-pause        # auto-swap at the checkpoint

# Claude Code
/prewalk Add a settings page with tabbed sections
/prewalk Refactor the auth module --no-pause
```

At the `⏸️ PAUSE` checkpoint, review the plan and task #1, then:
- **`/pw-go`** — inject the handoff note and switch to the executor model.
- **`/pw-revise <changes>`** — revise the plan on the planner instead.

## Style A vs Style B

Both hosts ship two handoff variants:

| | Style A (default) | Style B (appendix) |
|---|---|---|
| mechanism | `/model` mid-session switch | pinned-model subagent |
| executor gets | **raw trajectory** (reads + todos + edit #1) | handoff **summary** only |
| fidelity | high | lower |
| per-turn cost | re-sends whole trajectory (cache lost) | light, clean window |
| best for | short/medium tasks | long tasks |

## How the model switch happens

**Neither Codex nor Claude Code can switch the model from a hook** (no such API).
So `/pw-go` injects a handoff note that instructs the model to run
`/model <executor>` itself (the TUI parses queued slash commands). The executor
then inherits the same conversation/thread, so the effect is close to seamless.

For a fully programmatic switch: Codex app-server `turn/start { model }`; Claude
Code Agent SDK `setModel()` (v2.1.200+).

This is the opencode-prewalk shape, **not** the seamless Hermes shape — no host
here exposes a per-request rewrite middleware. (For true seamless routing, stand
up a Responses-API proxy in front of Codex via `openai_base_url`; out of scope.)

## Tech stack

- **Hooks/helpers**: Python 3, standard library only (`json`/`sys`/`re`/`pathlib`).
  Both hosts run hooks as `type:"command"` scripts (short-lived spawned processes),
  where Python's zero-compile story beats TypeScript (needs a runtime) and Rust
  (compilation for no runtime benefit).
- **Skills/agents**: Markdown + frontmatter (host-mandated).
- **Config**: JSON (Claude Code) / TOML (Codex) (host-mandated).
- **Engine**: `_shared/prewalk_core.py` — state machine `idle → frontier → paused →
  executor`, `isPauseTodo`, todo validation, preset parsing, frontier/handoff
  prompts. Each adapter vendors a copy under `hooks/_shared/` so a plugin install
  is self-contained; `hooks/_bootstrap.py` finds the core from any install layout.

## Layout

```text
prewalk/
├── _shared/prewalk_core.py        # shared engine (source of truth)
├── .agents/plugins/marketplace.json   # Codex marketplace manifest
├── claude-code/                   # Claude Code: skills + hooks + settings
│   ├── hooks/  _bootstrap _common _arm _pw  pause_detect edit_gate  _shared/
│   ├── skills/ prewalk  pw-go  pw-revise
│   ├── agents/prewalk-executor.md        # Style B
│   ├── settings.example.json  presets.example.json
│   └── README.md
├── codex/                         # Codex: plugin (installed via marketplace)
│   ├── .codex-plugin/plugin.json         # Codex plugin manifest
│   ├── hooks.json                        # hook registration (Stop + PreToolUse)
│   ├── scripts/  prewalk_pause.sh  prewalk_edit_gate.sh   # hook wrappers
│   ├── hooks/  _bootstrap _common _arm _pw  pause_detect edit_gate  _shared/
│   ├── skills/ prewalk  pw-go  pw-revise
│   ├── agents/prewalk-executor.toml      # Style B
│   ├── presets.example.toml
│   └── README.md
└── install.sh                     # installer (claude-code; codex stages presets)
```

## Verification

```sh
python3 -m py_compile _shared/prewalk_core.py codex/hooks/*.py claude-code/hooks/*.py

# drive a host hook directly:
echo '{"hook_event_name":"PostToolUse","session_id":"s1","tool_input":{"todos":[
  {"content":"a verify: t","status":"completed"},
  {"content":"b check: build","status":"pending"},
  {"content":"c verify: lint","status":"pending"},
  {"content":"⏸️ PAUSE","status":"in_progress"}]}}' \
  | python3 claude-code/hooks/pause_detect.py
```

Both adapters passed end-to-end (arm → frontier → paused → `/pw-go` → executor →
completion → restore) plus all guardrails (trivial path, one-left/zero-remaining
skip, edit-gate deny/allow). The Codex plugin was also verified by copying it to
an arbitrary location and running every script off the vendored core + `PLUGIN_ROOT`.

## Attribution

Technique: Can Bölük / Stencil, ["You only need the frontier model for one single
edit"](https://stencil.so/blog/prewalk). Reference implementations:
[westfable/hermes-prewalk](https://github.com/ildunari/hermes-prewalk) (MIT) and
[Daniel-97/opencode-prewalk](https://github.com/Daniel-97/opencode-prewalk) (MIT).
This repo's engine + adapters are original, MIT-licensed.
