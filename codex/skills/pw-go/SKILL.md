---
name: pw-go
description: Prewalk handoff — confirm the plan and spawn a fresh-context executor subagent on the configured cheap model so it can finish the remaining todos. Run this at the ⏸️ PAUSE checkpoint.
---

# /pw-go — hand off to the executor

Run this to advance the prewalk state machine and get the handoff instructions:

```bash
python3 hooks/_pw.py go "$CODEX_SESSION_ID"
```

It prints either a handoff note (if you are at the ⏸️ checkpoint) or a message
saying there is no active checkpoint. Read its output and follow it exactly.

## If it returned a handoff

1. **Call Codex's native `spawn_agent` tool exactly once.** Pass the 3–5 line
   frontier summary as `message`, pass the executor model named by the helper as
   `model`, and set `fork_context` to `false` so the executor starts fresh. The
   message must include the files read, the full todo list, what task #1 proved,
   exactly what remains, and the executor rules from the handoff note. Codex does
   not resolve `prewalk-executor` as a named plugin agent, so do not pass that
   string as a positional argument or rely on `agents/prewalk-executor.toml` for
   routing.
2. The executor agent will work the remaining todos **strictly in order, one at a
   time**, running each item's verification before marking it completed. It will
   imitate the style and verification cadence task #1 demonstrated, avoid re-reading
   files already summarized, and report what it finished and any item it could not
   complete when done.
3. An in-thread `/model <executor>` switch is available only as a fallback when
   `spawn_agent` is unavailable. If you use it, switch to the executor model the
   helper names, then continue the remaining todos **strictly in order, one at a
   time**, running each item's verification before marking it completed. Do not
   restart planning.

## If it said there is no active checkpoint

Reply with a single line saying so and end your turn. Do not touch the todo
list or any file.
