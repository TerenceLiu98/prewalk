# Prewalk for Codex

This plugin uses the active root Codex session to explore, plan, and complete
the first verified task, then hands a structured packet to a configured
executor. Presets never select or replace the root model.

See the repository [README](../README.md) for the user workflow and preset
schema. This document covers the Codex-specific adapter.

## Install

Python 3.10+ and Codex CLI 0.146.0+ are required.

```sh
codex plugin marketplace add TerenceLiu98/prewalk
codex plugin add prewalk@prewalk-marketplace
```

Restart Codex. `~/.codex/prewalk-presets.toml` is optional; copy
`presets.example.toml` there only when you need custom model routes.

## Use

```text
$prewalk:prewalk <task>
$prewalk:pw-go
```

Use `$prewalk:pw-revise <changes>` to change the plan. Operational skills are
`pw-status`, `pw-retry`, `pw-reconcile`, `pw-off`, `pw-doctor`, and the explicit
manual-model fallback `pw-resume`. `--fast` automatically requests the same
validated handoff at the Stop checkpoint.

## Capability-safe route

Codex hooks validate the native subagent request without rewriting it. `pw-go`
inspects the live `spawn_agent` schema before creating a one-time route:

```text
durable Stop checkpoint -> handoff_requested
  -> schema has model + fork_turns controls
     -> token-bound PreToolUse validation
     -> spawn once with fork_turns="none"
     -> PostToolUse binds returned agent_id -> executor_running
  -> required model control is absent
     -> retain checkpoint without spawning
```

Presets with `require_model_routing=true` never silently spawn on an unknown
model. A spawn failure remains retryable. Only the bound agent's SubagentStop
can finish the run. Executors report
`PREWALK_COMPLETE` or `PREWALK_INCOMPLETE: <reason>`; incomplete work restores
the checkpoint.

## Hooks

`hooks.json` registers:

- `PostToolUse` plan/todo tools: track the current snapshot.
- `PreToolUse`/`PostToolUse` `spawn_agent`: validate the exact route and bind
  the returned agent identity.
- `SubagentStop`: accept a final marker only from the bound executor.
- Root `Stop`: capture the exact checkpoint, trigger `--fast`, and recover an
  explicitly interrupted bound route.

Skills call `_arm.py` and `_pw.py`; hook entry points use shell wrappers because
Codex runs plugin hooks with the plugin root as the working directory.
All helpers prefer `CODEX_THREAD_ID`; hook payload `session_id` must match it.
Legacy callers may still pass an explicit id when `CODEX_THREAD_ID` is absent.
Prewalk never guesses identity from the newest rollout file.

## State

State lives in `~/.codex/prewalk-state.json`, or under `CODEX_HOME`. The plugin
uses locked atomic writes and quarantines malformed JSON with a `.corrupt`
suffix.

## Update

```sh
codex plugin marketplace upgrade prewalk-marketplace
codex plugin add prewalk@prewalk-marketplace
```

Restart Codex after updating.
