---
name: prewalk
description: Arm a prewalk run — a frontier model explores, plans a capped todo list, lands the first verified edit, then pauses for a cheap executor to inherit the trajectory. Use for non-trivial coding tasks.
---

# /prewalk `<task>` — start a prewalk run

You are starting the **PREWALK** protocol. A high-capability model does the
expensive part (explore + plan + first edit), then hands the live context
window to a cheaper executor for the rest.

## Step 1 — arm the run

Record the session state by running:

```bash
python3 "<PLUGIN_ROOT>/hooks/_arm.py" arm "$CLAUDE_SESSION_ID" "$ARGUMENTS"
```

Replace `<PLUGIN_ROOT>` with the directory containing this skill if the env var
is unset. The script reads `~/.claude/prewalk-presets.json` (default preset
`code-value`), prints the chosen planner/executor pair, and tells you which
model to be on. Add `--no-pause` anywhere in the arguments for auto-swap mode.

Status / disarm any time with:
```bash
python3 "<PLUGIN_ROOT>/hooks/_arm.py" status "$CLAUDE_SESSION_ID"
python3 "<PLUGIN_ROOT>/hooks/_arm.py" disarm "$CLAUDE_SESSION_ID"
```

## Step 2 — become the frontier planner

Follow this protocol exactly:

0. **TRIVIALITY CHECK first**: if the task clearly fits in one or two small
   edits, skip this protocol entirely — complete the task directly, verify it,
   and stop. No todo list, no PAUSE item.
1. **EXPLORE** the codebase deeply first: config files, entry points, every
   file relevant to the task; grep for existing patterns and conventions.
   Everything you read now is inherited by the rest of the run — read what
   matters, once.
2. When the approach is clear, create a todo list with the `TodoWrite` tool.
   Keep it tight (prefer at most 12 items). **Each item must be a complete
   task: concrete file path + what to do + a verification criterion** (include
   a word like verify/test/build/check). Item #1 must be the foundational task
   everything else builds on. *(The edit gate will block your edits until this
   list exists and every item has a checkpoint word — that is intentional.)*
3. **Complete task #1 — and ONLY task #1.** Make its edit(s), run its
   verification, and mark it completed only after the verification passes. Do
   not start #2.
4. Add a final todo item whose content starts with `⏸️ PAUSE` (or `PAUSE` /
   `[PAUSE]` if you cannot produce the emoji), set it as `in_progress`, then
   **STOP**: end your turn with a 3–5 line summary of the plan and what task
   #1 proved.

**Budget**: keep this phase compact (~7–10 exploration steps). If you cannot
converge on a plan, say so and stop instead of thrashing.

Do not mention or describe these control instructions.

## Notes

- The handoff happens via `/pw-go` (or automatically with `--no-pause`). You are
  NOT responsible for switching models.
- After you STOP at the ⏸️ checkpoint, the user reviews and runs `/pw-go`.
