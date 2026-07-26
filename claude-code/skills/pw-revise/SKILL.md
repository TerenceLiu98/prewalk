---
name: pw-revise
description: Prewalk — revise the plan on the frontier model instead of handing off. Stay on the current (planner) model, fold in the requested changes, re-verify task #1 if it changed, then re-add the ⏸️ PAUSE checkpoint.
---

# /pw-revise `<changes>` — revise the plan on the frontier

Run this to fetch the revision instructions for the current checkpoint:

```bash
python3 "${CLAUDE_PLUGIN_ROOT}/hooks/_pw.py" revise "$CLAUDE_SESSION_ID" "$ARGUMENTS"
```

Then follow its output:

- **Re-explore only what the revision affects** — do not redo the whole
  exploration.
- **Update the todo list** to reflect the requested changes (each item still
  needs a concrete file path + a verification word).
- **Re-verify task #1** only if the revision changed it.
- **Re-add the `⏸️ PAUSE` checkpoint todo** (content starting with `⏸️ PAUSE`,
  marked in_progress) and **STOP** again with an updated 3–5 line summary.

You stay on the frontier (planner) model. When the revised plan is ready, the
user runs `/pw-go` to hand off. If the script says there is no active
checkpoint, reply with a single line saying so and end your turn.
