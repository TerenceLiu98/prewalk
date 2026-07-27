---
name: pw-go
description: Request the reviewed prewalk handoff and spawn exactly one routed Claude Task for the remaining todos.
---

# Prewalk Handoff

Run:

```bash
python3 "${CLAUDE_PLUGIN_ROOT}/hooks/_pw.py" go "$CLAUDE_SESSION_ID"
```

If there is no active checkpoint, report that and stop. If a handoff is already
pending, do not spawn another Task.

For a new handoff request, spawn exactly one `Task`/`Agent` using the complete
structured Handoff Packet as its prompt. The PreToolUse hook rewrites that Task
to the configured `prewalk-executor` and executor model. Do not edit remaining
work in the main session and do not report success before the Task returns.

The PostToolUse hook owns the result:

- `PREWALK_COMPLETE` clears the run.
- `PREWALK_INCOMPLETE: <reason>` restores the checkpoint for revision or retry.
- rejection, failure, or a missing marker also restores a retryable checkpoint.
