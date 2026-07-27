---
name: pw-resume
description: Recover a Claude Prewalk handoff only when a successful executor Task result was observed but its PostToolUse hook did not run.
---

# Prewalk Resume

This is a recovery path, not the normal handoff. First verify that the prior
Task actually ran on the configured executor and inspect its final marker.

```bash
python3 "${CLAUDE_PLUGIN_ROOT}/hooks/_pw.py" resume "$CLAUDE_SESSION_ID"
```

Then run `_pw.py complete` only for `PREWALK_COMPLETE`, or `_pw.py incomplete
<reason>` for `PREWALK_INCOMPLETE`. Never confirm a rejected, missing, or
unverified Task result.
