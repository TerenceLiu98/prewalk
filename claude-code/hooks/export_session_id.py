#!/usr/bin/env python3
"""Claude Code SessionStart hook — expose the session id to Bash-tool commands.

Claude Code does NOT inject the session id into the Bash-tool subprocess
environment by default (see anthropics/claude-code#20132, still open). Hooks,
however, receive `session_id` in their stdin JSON. A SessionStart hook can
persist env vars into $CLAUDE_ENV_FILE, and anything written there becomes
available to every subsequent Bash command Claude Code runs in the session.

So this hook reads session_id from stdin and writes
`export CLAUDE_SESSION_ID="<id>"` into $CLAUDE_ENV_FILE. After it runs, the
/prewalk skill's `python3 _arm.py arm "$CLAUDE_SESSION_ID" ...` works.

Register on SessionStart (see settings.example.json). Always exits 0 — this
must never block a session from starting.
"""

from __future__ import annotations

import json
import os
import sys


def main() -> int:
    try:
        payload = json.load(sys.stdin)
    except Exception:
        payload = {}
    sid = str(payload.get("session_id") or payload.get("sessionId") or "")
    env_file = os.environ.get("CLAUDE_ENV_FILE")
    if sid and env_file:
        try:
            with open(env_file, "a", encoding="utf-8") as fh:
                fh.write(f'export CLAUDE_SESSION_ID="{sid}"\n')
        except OSError:
            pass  # never block session start
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
