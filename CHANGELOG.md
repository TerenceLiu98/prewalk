# Changelog

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
