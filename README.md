# prewalk for Codex and Claude Code

Prewalk lets a strong planner do the expensive repository work, then gives a
configured executor a verified starting point and a self-contained handoff:

```text
planner:  explore -> capped plan -> task 1 + verification -> PAUSE
                                                            |
                                              review or automatic fast mode
                                                            |
executor: structured packet -> remaining tasks -> verify -> COMPLETE / INCOMPLETE
```

Use it for changes where understanding the repository is a meaningful part of
the work. Skip it for a one-file fix or one or two small edits.

## Start in three steps

### Codex

```sh
codex plugin marketplace add TerenceLiu98/prewalk
codex plugin add prewalk@prewalk-marketplace
```

Restart Codex, select the planner reported by your preset, then run:

```text
/model gpt-5.6-sol
$prewalk:prewalk Add a settings page with tests
$prewalk:pw-go
```

Before spawning, `pw-go` inspects the available `spawn_agent` capability. When
the runtime cannot route an explicit executor model and the preset requires it,
Prewalk directs you to `/model <executor>` followed by `$prewalk:pw-resume`.

### Claude Code

```sh
claude plugin marketplace add TerenceLiu98/prewalk
claude plugin install prewalk@prewalk
```

Restart Claude Code, then run:

```text
/model opus
/prewalk:prewalk Add a settings page with tests
/prewalk:pw-go
```

Claude rewrites only the token-bearing Agent call, binds its lifecycle identity,
and accepts a completion marker only from that executor's `SubagentStop` event.

## Commands

| Action | Codex | Claude Code |
| --- | --- | --- |
| Start | `$prewalk:prewalk <task>` | `/prewalk:prewalk <task>` |
| Review and hand off | `$prewalk:pw-go` | `/prewalk:pw-go` |
| Revise the plan | `$prewalk:pw-revise <changes>` | `/prewalk:pw-revise <changes>` |
| Show state | `$prewalk:pw-status` | `/prewalk:pw-status` |
| Disarm | `$prewalk:pw-off` | `/prewalk:pw-off` |
| Diagnose setup | `$prewalk:pw-doctor` | `/prewalk:pw-doctor` |
| Resume manual/recovery handoff | `$prewalk:pw-resume` | `/prewalk:pw-resume` |

Clients may display shorter aliases or a leading slash, such as
`/$prewalk:prewalk`. These refer to the same namespaced skills.

Add `--preset <name>` before task text to select a model pair. Add `--fast`
(legacy alias: `--no-pause`) to skip human review after the checkpoint:

```text
$prewalk:prewalk --preset backend --fast Optimize the job queue
/prewalk:prewalk --preset frontend Rebuild the dashboard and verify screenshots
```

Fast mode still validates the checkpoint and confirms routing. It only removes
the wait for `pw-go`.

## What the planner must produce

The planner explores the relevant entry points, configuration, tests, and local
patterns. It creates at most the preset's `max_todos`; every real item includes
a test/build/verify/check criterion. It then completes and verifies only task 1.

The handoff is accepted only when the snapshot contains:

- a non-empty, capped todo list;
- task 1 marked `completed`;
- a `PAUSE for handoff` item;
- remaining work worth delegating.

The planner stops with a structured Handoff Packet:

```text
Goal
Files Read
Constraints And Existing Patterns
Full Todo List
Task 1 Changes
Verification Already Run
Remaining Work
Risks / Do Not Repeat
```

This packet, not the planner's raw context, becomes the executor's fresh
context. The executor must finish with `PREWALK_COMPLETE` or
`PREWALK_INCOMPLETE: <reason>`.

## Configuration

Preset files are optional for a first run; each host has built-in fallbacks.
Create one when the default model names do not exist in your environment or you
want multiple routes.

| Host | Optional preset file | Format |
| --- | --- | --- |
| Codex | `~/.codex/prewalk-presets.toml` | TOML |
| Claude Code | `~/.claude/prewalk-presets.json` | JSON |

`CODEX_HOME` and `CLAUDE_CONFIG_DIR` relocate both preset and state files.
Templates are in [codex/presets.example.toml](codex/presets.example.toml) and
[claude-code/presets.example.json](claude-code/presets.example.json).

A preset controls:

| Field | Meaning |
| --- | --- |
| `planner`, `executor` | Host-resolvable model names |
| `max_todos` | Maximum real tasks in the handoff plan |
| `planner_thinking`, `executor_thinking` | Requested effort when the host exposes that control |
| `handoff_mode` | `auto`, `spawn`, or `manual-model` |
| `require_model_routing` | Refuse an unpinned executor spawn when `true` |

Example TOML:

```toml
default_preset = "code-value"

[presets.code-value]
planner = "gpt-5.6-sol"
executor = "gpt-5.6-luna"
max_todos = 12
planner_thinking = "high"
executor_thinking = "medium"
handoff_mode = "auto"
require_model_routing = true
```

Thinking settings are capability declarations, not a promise that every host
API can apply them. `pw-doctor` reports what can be checked statically; Codex's
runtime spawn schema must be inspected at handoff time.

## Handoff and recovery

| | Codex | Claude Code |
| --- | --- | --- |
| Route | Native `spawn_agent`, explicit model and fresh context when supported | Token-bearing Agent input rewritten to executor model/subagent |
| Confirmation | Skill confirms only after spawn succeeds | PostToolUse acknowledges launch; bound SubagentStop owns the result |
| Failure | Restores `paused`; `pw-go` is retryable | Restores `paused`; `pw-go` is retryable |
| Incomplete executor | `pw-resume`, `pw-revise`, or retry | `pw-resume`, `pw-revise`, or retry |

`pw-status` shows the phase, model pair, routing attempts, checkpoint evidence,
remaining todo count, and last error. `pw-off` clears only Prewalk state; it does
not edit files or todos.

State is stored per session in `prewalk-state.json`. Writes use a cross-process
lock and atomic replacement. Malformed state is preserved as
`prewalk-state.json.corrupt` before recovery.

## Measure it locally

Prewalk does not send telemetry and does not claim a universal cost reduction.
Record comparable baseline and Prewalk runs locally:

```sh
python3 scripts/benchmark.py record runs.jsonl --mode baseline --task "settings page" \
  --input-tokens 12000 --output-tokens 3000 --duration-seconds 420 --passed
python3 scripts/benchmark.py record runs.jsonl --mode prewalk --task "settings page" \
  --input-tokens 8000 --output-tokens 2800 --duration-seconds 360 --passed
python3 scripts/benchmark.py report runs.jsonl
```

The report compares run count, pass rate, average total tokens, and duration.

## Development

The implementation uses Python 3.10+ and the standard library only. The
canonical state machine is `_shared/prewalk_core.py`; both plugins vendor an
identical copy.

```sh
./scripts/check.sh
```

The check runs unit and end-to-end tests, validates manifests and shell entry
points, compares shared-core copies, and smoke-installs both integrations. CI
runs it on Linux and macOS across supported Python versions.

Detailed host notes: [Codex](codex/README.md) and
[Claude Code](claude-code/README.md). Release history is in
[CHANGELOG.md](CHANGELOG.md).

## Attribution

The technique comes from Can Boluk / Stencil's
["You only need the frontier model for one single edit"](https://stencil.so/blog/prewalk).
Reference implementations and mechanisms include
[westfable/hermes-prewalk](https://github.com/ildunari/hermes-prewalk),
[Daniel-97/opencode-prewalk](https://github.com/Daniel-97/opencode-prewalk), and
[tzachbon/claude-model-router-hook](https://github.com/tzachbon/claude-model-router-hook).
This repository's shared engine and adapters are MIT-licensed.
