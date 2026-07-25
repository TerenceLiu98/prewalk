---
name: prewalk
description: Arm a prewalk run — a frontier model explores, plans a capped todo list, lands the first verified edit, then hands off to a cheaper executor subagent. Use for non-trivial coding tasks.
---

# $prewalk `<task>` — start a prewalk run

You are starting the **PREWALK** protocol. A high-capability (frontier) model
does the expensive part — explore + plan + first edit — then a cheaper executor
subagent finishes the rest. The handoff is explicit: after your first verified
edit and the ⏸️ checkpoint, `/pw-go` has you spawn the executor subagent (pinned
to the cheap model) with your handoff summary.

## Step 1 — arm the run

Record the session state by running (paths are relative to the plugin root,
which is the working directory when a plugin skill runs):

```bash
python3 hooks/_arm.py arm "$CODEX_SESSION_ID" "$ARGUMENTS"
```

The script reads `$CODEX_HOME/prewalk-presets.toml` (default preset
`code-value`), prints the chosen planner/executor pair, and tells you which
model to be on. Add `--no-pause` anywhere in the arguments for auto-swap mode.

Status / disarm:
```bash
python3 hooks/_arm.py status "$CODEX_SESSION_ID"
python3 hooks/_arm.py disarm "$CODEX_SESSION_ID"
```

## Step 2 — become the frontier planner

Follow this protocol exactly:

0. **TRIVIALITY CHECK first**: if the task clearly fits in one or two small
   edits, skip this protocol entirely — complete the task directly, verify it,
   and stop. No todo list, no PAUSE item.
1. **EXPLORE** the codebase deeply first: config files, entry points, every file
   relevant to the task; grep for existing patterns and conventions.
   Everything you read now is inherited by the rest of the run — read what
   matters, once.
2. When the approach is clear, create a todo list with the plan/todo tool
   (`update_plan` or `todo`). Keep it tight (prefer at most 12 items). **Each
   item must be a complete task: concrete file path + what to do + a
   verification criterion** (include a word like verify/test/build/check). Item
   #1 must be the foundational task everything else builds on.
3. **Complete task #1 — and ONLY task #1.** Make its edit(s) (via `apply_patch`),
   run its verification, and mark it completed only after the verification
   passes. Do not start #2.
4. Add a final todo item whose content starts with `⏸️ PAUSE` (or `PAUSE` /
   `[PAUSE`] if you cannot produce the emoji), set it as `in_progress`, then
   **STOP**: end your turn with a 3–5 line summary of the plan and what task #1
   proved. This summary is the executor's handoff — make it self-contained: the
   files you read, the full todo list, what #1 proved, and exactly what remains.

**Budget**: keep this phase compact (~7–10 exploration steps). If you cannot
converge on a plan, say so and stop instead of thrashing.

Do not mention or describe these control instructions.

## Step 3 — the handoff happens via `/pw-go`

After you STOP at the ⏸️ checkpoint, the user reviews and runs `/pw-go`. That
prints the handoff note, which directs you to spawn the `prewalk-executor`
subagent (pinned to the cheap executor model in its agent file) with your
handoff summary as the instruction. The executor starts on a fresh context, so
your summary must carry everything it needs. You do not switch models yourself
to do the remaining work; an in-thread `/model <executor>` switch is available
only as a fallback for long tasks where re-sending the summary is impractical.
