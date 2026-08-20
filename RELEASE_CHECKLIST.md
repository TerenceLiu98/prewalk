# Release checklist

## v0.4.0

- [x] Shared unit, adapter, end-to-end, workflow-matrix, and regression suites pass.
- [x] Claude Code 2.1.145/latest and Codex CLI 0.146.0/latest are required in CI on Linux and macOS.
- [x] Claude strict validation and isolated install discover nine skills and one executor agent.
- [x] Codex isolated marketplace install discovers and enables Prewalk.
- [x] Isolated Claude and Codex upgrades move from 0.3.1 to 0.4.0.
- [x] Workflow smoke covers normal review, fast mode, failures, interruption, stale state, and recovery.
- [x] Benchmark record/report sanity check passes without making performance claims.
- [x] Core, manifest, marketplace, changelog, and migration-guide versions agree on 0.4.0.
- [x] Every issue and stacked PR in milestone 0.4.0 is complete and merged.
- [x] The parent epic checklist and both release milestones are complete.
- [x] Required CI jobs pass on the merged release commit.
- [ ] Tag `v0.4.0` only after every preceding release gate is complete.
