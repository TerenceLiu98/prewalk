---
name: pw-resume
description: Confirm a manual-model Prewalk handoff after switching the current Codex thread to the configured executor model.
---

# $prewalk:pw-resume - continue after a manual model switch

Run this only after `pw-go` requested the manual fallback and the user completed
`/model <executor>`:

```bash
python3 hooks/_pw.py resume "${CODEX_THREAD_ID:-${CODEX_SESSION_ID:-}}"
```

If confirmation succeeds, continue the remaining todos in the current thread,
strictly in order. Use the structured Handoff Packet already in the conversation;
do not restart exploration or repeat task 1. Verify every item before completing
it. Run `_pw.py complete` when done or `_pw.py incomplete <reason>` when work
remains.
