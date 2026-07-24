---
name: pw-go
description: Prewalk handoff — confirm the plan and switch to the cheaper executor model so it can finish the remaining todos on the inherited trajectory. Run this at the ⏸️ PAUSE checkpoint.
---

# /pw-go — hand off to the executor

Run this to advance the prewalk state machine and get the handoff instructions:

```bash
python3 "<PLUGIN_ROOT>/hooks/_pw.py" go "$CLAUDE_SESSION_ID"
```

It prints either a handoff note (if you are at the ⏸️ checkpoint) or a message
saying there is no active checkpoint. Read its output and follow it exactly.

## If it returned a handoff

1. **Switch the session to the executor model now** by running the model-switch
   command the script names (e.g. `/model haiku`). This is what makes prewalk
   cheap: the executor inherits the whole trajectory — the reads, the todo list,
   and the verified first edit — and finishes the rest.
2. Then continue the work yourself: check off the `⏸️ PAUSE` todo and work the
   remaining todos **strictly in order, one at a time**, running each item's
   verification before marking it completed. Imitate the style and verification
   cadence task #1 demonstrated. Do not re-read files already in the
   conversation. Do not restart planning.
3. When every real todo is completed (or explicitly cancelled with a reason),
   report what you finished and any item you could not complete.

## If it said there is no active checkpoint

Reply with a single line saying so and end your turn. Do not touch the todo
list or any file.
