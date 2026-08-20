# Prewalk for Codex

This plugin uses a strong Codex model to explore, plan, and complete the first
verified task, then hands a structured packet to a configured executor.

See the repository [README](../README.md) for the user workflow and preset
schema. This document covers the Codex-specific adapter.

## Install

Python 3.10+ must be available as `python3`.

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
`pw-status`, `pw-off`, `pw-doctor`, and `pw-resume`. `--fast` automatically
requests the same validated handoff at the Stop checkpoint.

## Capability-safe route

Codex hooks cannot rewrite a native subagent request. After `pw-go`, the skill
inspects the runtime `spawn_agent` schema:

```text
validated PAUSE -> handoff_requested
  -> schema has model + fork_turns controls
     -> spawn once with fork_turns="none" -> confirm -> executor
  -> required model control is absent
     -> mark spawn attempt failed -> restore paused
     -> /model <executor> -> pw-resume
```

Presets with `require_model_routing=true` never silently spawn on an unknown
model. A spawn failure remains retryable. Executors report
`PREWALK_COMPLETE` or `PREWALK_INCOMPLETE: <reason>`; incomplete work restores
the checkpoint.

## Hooks

`hooks.json` registers:

- `PostToolUse` plan/todo tools: track the current snapshot.
- `PostToolUse` direct edit, shell, and RepoPrompt tools: observe a real first
  mutation while rejecting quoted/commented `apply_patch`, failures, and no-op.
- `Stop`: validate the checkpoint, trigger `--fast`, and clean trivial runs.

Skills call `_arm.py` and `_pw.py`; hook entry points use shell wrappers because
Codex runs plugin hooks with the plugin root as the working directory.

## State

State lives in `~/.codex/prewalk-state.json`, or under `CODEX_HOME`. The plugin
uses locked atomic writes and quarantines malformed JSON with a `.corrupt`
suffix.

## Update

```sh
codex plugin marketplace upgrade prewalk-marketplace
```
