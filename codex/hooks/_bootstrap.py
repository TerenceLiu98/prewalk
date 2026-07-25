"""Locate prewalk_core.py regardless of install layout.

Imported by every hook/helper script (pause_detect, edit_tracker, todo_tracker,
_arm, _pw) so they all share one path-resolution strategy. This is what makes the
plugin installable from GitHub (via `codex plugin install <repo>`) or as loose
files: the core engine is found whether it lives at <repo>/_shared, inside a
vendored hooks/_shared, or under PLUGIN_ROOT.

Usage at the top of a script, before `import prewalk_core`:

    import _bootstrap  # noqa: F401  (finds core, no symbol needed)
    import prewalk_core as core
"""

from __future__ import annotations

import os
import sys
from pathlib import Path


def _find_core() -> None:
    here = Path(__file__).resolve().parent
    candidates: list[Path] = []

    env = os.environ.get("PREWALK_CORE")
    if env:
        candidates.append(Path(env))

    # Repo layouts (this file is in <pkg>/hooks/):
    candidates.extend([
        here.parent.parent / "_shared",   # <root>/_shared        (repo root)
        here.parent / "_shared",          # <pkg>/_shared
        here / "_shared",                 # <pkg>/hooks/_shared   (vendored)
    ])

    plugin_root = os.environ.get("PLUGIN_ROOT") or os.environ.get("CLAUDE_PLUGIN_ROOT")
    if plugin_root:
        pr = Path(plugin_root)
        candidates.extend([pr / "_shared", pr.parent / "_shared", pr, pr / "hooks" / "_shared"])

    for c in candidates:
        try:
            if c.is_dir() and (c / "prewalk_core.py").is_file():
                s = str(c)
                if s not in sys.path:
                    sys.path.insert(0, s)
                return
        except OSError:
            continue

    # Last resort: search up to a few levels up for prewalk_core.py.
    node = here
    for _ in range(5):
        try:
            for hit in node.rglob("prewalk_core.py"):
                s = str(hit.parent)
                if s not in sys.path:
                    sys.path.insert(0, s)
                return
        except OSError:
            pass
        if node.parent == node:
            break
        node = node.parent


_find_core()
