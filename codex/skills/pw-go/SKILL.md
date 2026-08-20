---
name: pw-go
description: Request a capability-safe executor handoff from a durable Prewalk Stop checkpoint.
---

# $prewalk:pw-go - hand off to the executor

Run the state transition helper and follow its output exactly:

```bash
python3 hooks/_pw.py go "${CODEX_THREAD_ID:-${CODEX_SESSION_ID:-}}"
```

If no active checkpoint exists, report that in one line and stop.

## Native spawn path

1. Inspect the actual `spawn_agent` schema. If the helper requires model routing
   and the schema has no `model` parameter, do not spawn. Tell the user to run
   `/model <executor>` and then `$prewalk:pw-resume`.
2. When routing is supported, call `spawn_agent` exactly once with the generated
   `task_name`, the complete structured Handoff Packet as `message`,
   `fork_turns: "none"`, and the configured executor `model`. Include
   `reasoning_effort` only when the exposed schema supports it. Do not use a
   named plugin agent.
3. If spawning fails, restore the checkpoint immediately:

```bash
python3 hooks/_pw.py fail "${CODEX_THREAD_ID:-${CODEX_SESSION_ID:-}}" "<failure reason>"
```

4. Only after the spawn call succeeds, confirm it:

```bash
python3 hooks/_pw.py confirm "${CODEX_THREAD_ID:-${CODEX_SESSION_ID:-}}"
```

5. Wait for the executor. Its report ends with `PREWALK_COMPLETE` or
   `PREWALK_INCOMPLETE: <reason>`. Record that result:

```bash
python3 hooks/_pw.py complete "${CODEX_THREAD_ID:-${CODEX_SESSION_ID:-}}"
python3 hooks/_pw.py incomplete "${CODEX_THREAD_ID:-${CODEX_SESSION_ID:-}}" "<reason>"
```

Incomplete work retains the checkpoint so the user can run `pw-go` or `pw-revise`
again. Never mark a handoff complete before the relevant tool reports success.
