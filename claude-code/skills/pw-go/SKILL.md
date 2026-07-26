---
name: pw-go
description: Prewalk handoff — confirm the plan and switch to the cheaper executor model so it can finish the remaining todos on the inherited trajectory. Run this at the ⏸️ PAUSE checkpoint.
---

# /pw-go — hand off to the executor

Run this to advance the prewalk state machine and get the handoff instructions:

```bash
python3 "${CLAUDE_PLUGIN_ROOT}/hooks/_pw.py" go "$CLAUDE_SESSION_ID"
```

It prints either a handoff note (if you are at the ⏸️ checkpoint) or a message
saying there is no active checkpoint. Read its output and follow it exactly.

## If it returned a handoff

1. Spawn exactly one Task (Agent tool) with the complete handoff summary as its
   prompt. The prewalk hook rewrites that spawn onto the configured executor
   model and `prewalk-executor` subagent.
2. Do not switch the main session's model and do not perform the remaining edits
   in the main session. Let the executor finish the todos and report its result.

## If it said there is no active checkpoint

Reply with a single line saying so and end your turn. Do not touch the todo
list or any file.
