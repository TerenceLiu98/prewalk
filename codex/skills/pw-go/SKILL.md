---
name: pw-go
description: Prewalk handoff — confirm the plan and spawn the executor subagent (pinned to the cheap model) so it can finish the remaining todos on the inherited trajectory. Run this at the ⏸️ PAUSE checkpoint.
---

# /pw-go — hand off to the executor

Run this to advance the prewalk state machine and get the handoff instructions:

```bash
python3 hooks/_pw.py go "$CODEX_SESSION_ID"
```

It prints either a handoff note (if you are at the ⏸️ checkpoint) or a message
saying there is no active checkpoint. Read its output and follow it exactly.

## If it returned a handoff

1. **Spawn the executor subagent now** by running `spawn_agent("prewalk-executor",
   <handoff summary>)` where the handoff summary is your 3–5 line frontier phase
   output. The executor subagent is pinned to the cheap executor model (configured
   in its agent file). This is what makes prewalk cheap: the executor starts on a
   fresh context but receives your handoff summary — the reads, the full todo list,
   what task #1 proved, and exactly what remains — and finishes the rest.
2. The executor agent will work the remaining todos **strictly in order, one at a
   time**, running each item's verification before marking it completed. It will
   imitate the style and verification cadence task #1 demonstrated, avoid re-reading
   files already summarized, and report what it finished and any item it could not
   complete when done.
3. An in-thread `/model <executor>` switch is available as a fallback for long
   tasks where re-sending the summary to a new agent is impractical. If you use
   this fallback, switch to the executor model the script names, then continue the
   work yourself: check off the `⏸️ PAUSE` todo and work the remaining todos
   **strictly in order, one at a time**, running each item's verification before
   marking it completed. Do not restart planning.

## If it said there is no active checkpoint

Reply with a single line saying so and end your turn. Do not touch the todo
list or any file.
