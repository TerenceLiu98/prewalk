---
name: prewalk
description: "Arm a prewalk run: explore and plan on a frontier model, land the first verified edit, then pause for an explicit executor handoff."
---

# Prewalk

Arm the current session:

```bash
python3 "${CLAUDE_PLUGIN_ROOT}/hooks/_arm.py" arm "$CLAUDE_SESSION_ID" "$ARGUMENTS"
```

Options must precede task text: `--preset <name>` selects a preset and
`--fast` enables automatic handoff after validation (`--no-pause` is a legacy
alias). Follow the reported planner model and
thinking setting only through controls the host actually supports.

If the task is clearly one or two small edits, finish it directly. Otherwise:

1. Explore the relevant entry points, configuration, tests, and existing patterns.
2. Create at most the configured number of todos. Every real todo names concrete
   files or behavior and includes a test/build/verify/check criterion.
3. Add a final `PAUSE for handoff` todo.
4. Complete and verify only real task #1.
5. Update the todo snapshot so task #1 is `completed` and the PAUSE item is present.
6. Stop with this exact structured packet. Do not start task #2.

```text
Goal:
Files Read:
Constraints And Existing Patterns:
Full Todo List:
Task 1 Changes:
Verification Already Run:
Remaining Work:
Risks / Do Not Repeat:
```

Do not claim that handoff occurred. `/prewalk:pw-go` requests it; the bound
executor's `SubagentStop` hook confirms its final result.
