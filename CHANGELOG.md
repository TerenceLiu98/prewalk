# Changelog

## 0.4.0

- Keep the active Codex or Claude Code root session as the planner; presets now
  configure only the executor model, effort request, and routing policy.
- Replace model-authored pause sentinels with a validated root `Stop` checkpoint
  containing real todos, verification evidence, and a durable handoff packet.
- Persist a workspace- and session-scoped v4 state machine with atomic writes,
  exact route tokens, executor identity binding, retry, reconcile, and stale
  detection that never terminates an unknown agent.
- Route Codex through native `spawn_agent` fields and Claude Code through a
  token-bound Agent call plus native SubagentStart/SubagentStop lifecycle.
- Add `pw-retry` and `pw-reconcile`, expanded doctor/status diagnostics, and a
  Linux/macOS minimum/latest native CLI integration matrix.
- Reset v3 state intentionally during upgrade. See [MIGRATION.md](MIGRATION.md).

## 0.3.1

- Repair Claude plugin frontmatter and document all seven namespaced skills.
- Bind Claude executor results to a one-time token, routed tool call, and native
  SubagentStart/SubagentStop identity instead of treating Agent PostToolUse as completion.
- Bind Codex state to `CODEX_THREAD_ID` and remove the unsafe latest-rollout fallback.
- Emit the native Codex `spawn_agent` shape with `task_name`, `message`,
  `fork_turns: "none"`, explicit model, and optional supported reasoning effort.
- Add minimum/latest native CLI validation, isolated plugin discovery, lifecycle
  fixtures, and cross-session regression coverage on Linux and macOS.

## 0.3.0

- Require a validated checkpoint and structured Handoff Packet.
- Confirm Codex and Claude handoffs only after the executor route succeeds.
- Restore failed and incomplete handoffs to a retryable paused state.
- Detect real mutations from direct edit, shell `apply_patch`, and RepoPrompt tools.
- Add `pw-status`, `pw-off`, `pw-doctor`, `pw-resume`, and automatic `--fast` handoff.
- Add planner/executor thinking and routing capability fields to presets.
- Add cross-platform CI and an opt-in local baseline/prewalk benchmark tool.

## 0.2.1

- Harden per-session state storage and rewrite the quick-start documentation.
