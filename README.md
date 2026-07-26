# prewalk for Codex and Claude Code

Prewalk uses a strong model for the expensive part of a coding task, then hands
the remaining implementation to a cheaper model:

```text
strong planner -> explore -> plan -> first verified edit -> PAUSE
                                                           |
                                                     user reviews
                                                           |
                                                           v
handoff summary -> cheap executor -> finish and verify the remaining tasks
```

Use it for non-trivial work where understanding the repository is expensive.
For a one-file fix or one or two small edits, use your normal coding workflow.

## The three commands

| Action | Codex | Claude Code |
| --- | --- | --- |
| Start a run | `$prewalk:prewalk <task>` | `/prewalk <task>` |
| Accept the plan and hand off | `$prewalk:pw-go` | `/pw-go` |
| Revise the plan first | `$prewalk:pw-revise <changes>` | `/pw-revise <changes>` |

Codex may display short aliases such as `$prewalk` and `/pw-go`. Some clients
also add a leading `/` to namespaced skill calls, for example
`/$prewalk:prewalk`. These invoke the same plugin skills.

## Quick start: Codex

Prerequisite: Python 3 must be available as `python3`.

### 1. Install

```sh
codex plugin marketplace add TerenceLiu98/prewalk
codex plugin add prewalk@prewalk-marketplace
```

Restart Codex after installation.

### 2. Start a task

The default Codex preset uses `gpt-5.6-sol` as planner and `gpt-5.6-luna` as
executor. Select the planner before starting the run:

```text
/model gpt-5.6-sol
$prewalk:prewalk Add a settings page with tabbed sections and tests
```

The planner will explore the repository, create a capped todo list, complete
and verify only task 1, then stop at a `PAUSE` checkpoint.

### 3. Review and continue

If the plan and first edit look right:

```text
$prewalk:pw-go
```

To change the plan instead:

```text
$prewalk:pw-revise Add a migration rollback test before the API work
```

Update an existing installation with:

```sh
codex plugin marketplace upgrade prewalk-marketplace
```

## Quick start: Claude Code

Prerequisite: Python 3 must be available as `python3`.

### 1. Install

```sh
claude plugin marketplace add TerenceLiu98/prewalk
claude plugin install prewalk@prewalk
```

Restart Claude Code after installation.

### 2. Start a task

The built-in Claude Code fallback uses `opus` as planner and `haiku` as
executor:

```text
/model opus
/prewalk Add a settings page with tabbed sections and tests
```

### 3. Review and continue

```text
/pw-go
```

Or revise the plan before handoff:

```text
/pw-revise Add a migration rollback test before the API work
```

Update an existing installation with:

```sh
claude plugin marketplace update prewalk
```

## Configure model presets

Configuration is optional for the first run. Add it when the built-in model
names do not exist in your environment or when you want multiple planner and
executor pairs.

| Host | Config file | Format |
| --- | --- | --- |
| Codex | `~/.codex/prewalk-presets.toml` | TOML |
| Claude Code | `~/.claude/prewalk-presets.json` | JSON |

If `CODEX_HOME` or `CLAUDE_CONFIG_DIR` is set, prewalk stores the corresponding
config and state files there instead.

### Codex preset example

```toml
default_preset = "code-value"

[presets.code-value]
description = "Strong planner, cheaper executor"
planner = "gpt-5.6-sol"
executor = "gpt-5.6-luna"
max_todos = 12
```

The full template is in
[`codex/presets.example.toml`](codex/presets.example.toml).

### Claude Code preset example

```json
{
  "default": "code-value",
  "presets": {
    "code-value": {
      "description": "Strong planner, cheaper executor",
      "planner": "opus",
      "executor": "haiku",
      "max_todos": 12
    }
  }
}
```

The full template is in
[`claude-code/presets.example.json`](claude-code/presets.example.json).

Select a non-default preset by placing the option before the task text:

```text
$prewalk:prewalk --preset backend Optimize the job queue
/prewalk --preset frontend Rebuild the dashboard from the reference screenshots
```

Task words are never interpreted as preset names. `--no-pause` is intended only
for callers that already provide their own automatic handoff integration.

## What happens during a run

1. The planner checks whether the task is too small for prewalk.
2. It reads the relevant entry points, configuration, tests, and local patterns.
3. It creates at most the preset's `max_todos`; every task includes a concrete
   verification step.
4. It completes and verifies task 1 only.
5. It writes a self-contained handoff summary and stops for review.
6. After `pw-go`, a fresh executor context finishes the remaining tasks in
   order, verifying each one before marking it complete.

The checkpoint is deliberate. It lets you catch a bad plan while the strong
planner still owns the context. Use `pw-revise` when the plan needs correction;
do not start the remaining edits manually before handoff.

## How handoff differs by host

| | Codex | Claude Code |
| --- | --- | --- |
| Who starts the executor? | The model calls native `spawn_agent` after `pw-go` | A hook rewrites the next Task spawn |
| Executor context | Fresh context plus handoff summary | Fresh subagent plus handoff summary |
| Model routing | `spawn_agent(model=..., fork_context=false)` | `PreToolUse.updatedInput` forces the configured model |

Some Codex runtimes do not expose a `model` argument on `spawn_agent`. The
fresh-context handoff can still run, but the configured executor model cannot be
guaranteed. In that case, use the documented fallback: switch to the executor
with `/model <executor>` and continue the remaining todos in the current thread.

Neither host gives the executor the planner's raw context. The handoff summary
contains the files read, the full plan, what task 1 proved, and exactly what
remains. This is what lets the executor continue without repeating broad
exploration.

## Why it saves cost

Repository exploration is often more expensive than the final edits. A cheap
executor that starts from only a plan may need to read the whole codebase again.
Prewalk has the strong planner establish the implementation pattern with one
verified edit, then gives the executor a focused summary and a pattern to
imitate.

The technique comes from Can Boluk / Stencil's article
["You only need the frontier model for one single edit"](https://stencil.so/blog/prewalk).

## State and recovery

Each host stores per-session state next to its preset file:

- Codex: `~/.codex/prewalk-state.json`
- Claude Code: `~/.claude/prewalk-state.json`

Hook processes coordinate through a cross-process lock and atomic replacement.
If a state file is malformed, prewalk preserves it as
`prewalk-state.json.corrupt` and starts cleanly on the next update.

Detailed host documentation:

- [Codex implementation and hooks](codex/README.md)
- [Claude Code implementation and hooks](claude-code/README.md)

## Development

The implementation uses Python 3 and the standard library only. The canonical
state machine is `_shared/prewalk_core.py`; each plugin vendors an identical
copy under `hooks/_shared/`.

Run the complete repository check with:

```sh
./scripts/check.sh
```

It runs unit and end-to-end tests, checks all shared-engine copies, validates
JSON and shell entry points, and smoke-installs both host integrations.

## Repository layout

```text
prewalk/
|-- _shared/prewalk_core.py          shared state machine
|-- codex/                           Codex plugin, hooks, skills, presets
|-- claude-code/                     Claude Code plugin, hooks, skills, presets
|-- scripts/check.sh                 repository verification
|-- tests/                           core, adapter, install, and E2E tests
`-- install.sh                       loose-install helper
```

## Attribution

Reference implementations and mechanisms:

- [westfable/hermes-prewalk](https://github.com/ildunari/hermes-prewalk) (MIT)
- [Daniel-97/opencode-prewalk](https://github.com/Daniel-97/opencode-prewalk) (MIT)
- [tzachbon/claude-model-router-hook](https://github.com/tzachbon/claude-model-router-hook) (MIT)

This repository's shared engine and host adapters are original and MIT-licensed.
