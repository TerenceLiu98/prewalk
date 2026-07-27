#!/usr/bin/env python3
"""Claude Stop hook: request the automatic handoff for fast-mode checkpoints."""

from __future__ import annotations

import _bootstrap  # noqa: F401
import _common  # type: ignore[import-not-found]
import prewalk_core as core  # noqa: E402


def main() -> int:
    payload = _common.read_input()
    sid = _common.session_id(payload)
    store = _common.store_file()
    action = core.on_fast_handoff(store, sid, host="claude")
    _common.emit(action or core.on_turn_end(store, sid), event="Stop")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
