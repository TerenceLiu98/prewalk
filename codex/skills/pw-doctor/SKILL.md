---
name: pw-doctor
description: Diagnose the Prewalk installation, presets, hook manifest, state directory, and Codex model-routing capability.
---

# Prewalk Doctor

Run:

```bash
python3 hooks/_arm.py doctor "${CODEX_THREAD_ID:-${CODEX_SESSION_ID:-}}"
```

Then inspect the runtime `spawn_agent` tool schema. Report whether it exposes an
explicit executor `model` argument and a fresh-context option. Do not call the
tool. A missing model argument means spawn handoff cannot honor presets with
`require_model_routing=true`; recommend the manual model + `pw-resume` fallback.
Also report a missing or mismatched `CODEX_THREAD_ID`. Native session binding
requires Codex CLI 0.146.0 or newer; after upgrading, restart the Codex thread.
