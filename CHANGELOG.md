# Changelog

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
