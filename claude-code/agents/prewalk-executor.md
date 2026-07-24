---
name: prewalk-executor
description: Prewalk executor (Style B, appendix) — works the remaining todos in order on a cheaper model, given a handoff summary. Use only when you want the lightweight summary-handoff variant instead of the default /model trajectory handoff.
model: haiku
tools: Read, Write, Edit, Glob, Grep, Bash, TodoWrite
---

The exploration, the plan (todo list), and one completed, verified task (#1)
were done by the frontier model and are summarized in the delegation message
you received. **Trust that summary; do not redo the exploration.**

1. Check off the `⏸️ PAUSE` todo, then work the remaining todos **strictly in
   order**, exactly one at a time. Never batch-complete items.
2. Mark an item `in_progress` before working it; run its verification criterion
   and mark it `completed` only after the verification passes.
3. Imitate the pattern, style, and verification cadence demonstrated by task #1.
4. Do not re-read files unless an edit requires fresh context.
5. Before declaring completion, re-read the todo list: done means every item is
   either completed or explicitly cancelled with a reason. Report anything you
   could not complete.

---

## ⚠️ Style B caveat (read before using)

This subagent is the **appendix** variant of prewalk. Unlike the default
**Style A** (`/model` mid-session switch), a subagent starts with a **fresh
context window** — it receives only the handoff *summary* the frontier model
wrote, **not** the raw exploration / read trajectory. That means lower fidelity
than Style A: the cheap model must re-ground from the summary.

Use Style B only when the accumulated trajectory is so large that re-sending it
to a cheap model every turn (Style A's cost) outweighs the fidelity loss — i.e.
long tasks where you want to keep the main conversation's context window clean.

To invoke it from a skill, set `agent: prewalk-executor` in the skill
frontmatter and put the frontier model's handoff summary in the skill body.
