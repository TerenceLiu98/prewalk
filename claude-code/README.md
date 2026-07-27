# Prewalk for Claude Code

This plugin uses a strong Claude model to explore, plan, and complete the first
verified task, then routes one fresh Task to a configured executor.

See the repository [README](../README.md) for the user workflow and preset
schema. This document covers the Claude-specific adapter.

## Install

Python 3.10+ must be available as `python3`.

```sh
claude plugin marketplace add TerenceLiu98/prewalk
claude plugin install prewalk@prewalk
```

Restart Claude Code. `~/.claude/prewalk-presets.json` is optional; copy
`presets.example.json` there only when you need custom model routes.

## Use

```text
/prewalk <task>
/pw-go
```

Use `/pw-revise <changes>` instead of `/pw-go` to change the plan. Operational
skills are `/pw-status`, `/pw-off`, `/pw-doctor`, and recovery-only
`/pw-resume`. `--fast` on `/prewalk` automatically requests the handoff at the
validated Stop checkpoint.

## Two-phase route

Claude hooks cannot change the main session model. Prewalk therefore uses:

```text
/pw-go
  -> state = handoff_requested
Task PreToolUse
  -> updatedInput.model = executor
  -> updatedInput.subagent_type = prewalk:prewalk-executor
  -> state remains handoff_requested
Task PostToolUse
  -> success + PREWALK_COMPLETE: clear state
  -> success + PREWALK_INCOMPLETE: restore paused checkpoint
  -> failure/rejection/missing marker: restore paused checkpoint
```

The router never claims success before the Task result. The executor receives a
structured packet rather than the planner's raw context.

## Hooks

`hooks/hooks.json` registers:

- `SessionStart`: expose the session id to skill subprocesses.
- `PostToolUse` todo tools: validate checkpoints and track remaining work.
- `PostToolUse` edit/Bash/RepoPrompt tools: observe a real first mutation.
- `PreToolUse` Task/Agent: route a requested handoff.
- `PostToolUse` Task/Agent: confirm complete or incomplete results.
- `PostToolUseFailure` and `PermissionDenied` Task/Agent: restore failed routes.
- `Stop`: trigger `--fast` handoff and clean trivial runs.

The mutation adapter rejects failed/no-op responses and ignores `apply_patch`
text inside shell quotes or comments.

## State

State lives in `~/.claude/prewalk-state.json`, or under `CLAUDE_CONFIG_DIR`.
The plugin uses locked atomic writes and quarantines malformed JSON with a
`.corrupt` suffix.

## Update

```sh
claude plugin marketplace update prewalk
```
