---
name: pw-resume
description: "Recover a Claude Prewalk handoff only after independently verifying a missing executor lifecycle event."
---

# Prewalk Resume

This is a recovery path, not the normal handoff. First verify that the prior
Task ran on the configured executor and inspect its final marker. Never use an
unrelated Agent result to recover this run.

```bash
python3 "${CLAUDE_PLUGIN_ROOT}/hooks/_pw.py" resume "$CLAUDE_SESSION_ID"
```

Then run `_pw.py complete` only for `PREWALK_COMPLETE`, or `_pw.py incomplete
<reason>` for `PREWALK_INCOMPLETE`. Never confirm a rejected, missing, or
unverified Task result.
