from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "_shared"))

import prewalk_core as core  # noqa: E402


class PrewalkCoreTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp_dir.cleanup)
        self.store = Path(self.temp_dir.name) / "state.json"
        self.session_id = "test-session"
        self.preset = core.Preset(
            name="test",
            planner_model="planner-model",
            executor_model="executor-model",
        )

    def start_ready_run(self) -> core.PrewalkState:
        state = core.start_run(
            self.store,
            self.session_id,
            self.preset,
            auto_swap=False,
        )
        state.phase = core.READY
        state.first_edit_landed = True
        core.save_state(self.store, state)
        return state

    def test_ready_checkpoint_stays_ready_and_surfaces_pause_hint(self) -> None:
        self.start_ready_run()
        todos = [
            core.Todo("1", "Update core and test", "completed"),
            core.Todo("2", "Update adapters and verify", "pending"),
            core.Todo("3", "Update docs and check", "pending"),
            core.Todo("pause", "PAUSE for handoff", "in_progress"),
        ]

        action = core.on_todos_changed(self.store, self.session_id, todos)

        self.assertIsNotNone(action)
        self.assertEqual(action.system_message, core.PAUSED_HINT)
        state = core.load_state(self.store, self.session_id)
        self.assertIsNotNone(state)
        self.assertEqual(state.phase, core.READY)
        self.assertEqual(state.todos_remaining, 2)

    def test_codex_handoff_is_explicit_and_idempotent(self) -> None:
        self.start_ready_run()

        action = core.on_pw_go(self.store, self.session_id, host="codex")

        self.assertIn("`model` set to `executor-model`", action.additional_context)
        self.assertIn("`fork_context` set to `false`", action.additional_context)
        state = core.load_state(self.store, self.session_id)
        self.assertIsNotNone(state)
        self.assertEqual(state.phase, core.EXECUTOR)
        self.assertTrue(state.handoff_done)

        duplicate = core.on_pw_go(self.store, self.session_id, host="codex")
        self.assertIn("already handed off", duplicate.additional_context)

    def test_claude_handoff_waits_for_router_confirmation(self) -> None:
        self.start_ready_run()

        action = core.on_pw_go(self.store, self.session_id, host="claude")

        self.assertIn("spawning ONE Task", action.additional_context)
        state = core.load_state(self.store, self.session_id)
        self.assertIsNotNone(state)
        self.assertEqual(state.phase, core.READY)
        self.assertFalse(state.handoff_done)

    def test_corrupt_store_is_quarantined_and_recovers(self) -> None:
        self.store.write_text('{"broken":', encoding="utf-8")

        self.assertIsNone(core.load_state(self.store, self.session_id))
        backup = self.store.with_name(self.store.name + ".corrupt")
        self.assertEqual(backup.read_text(encoding="utf-8"), '{"broken":')

        state = core.start_run(self.store, self.session_id, self.preset, auto_swap=False)
        self.assertEqual(core.load_state(self.store, self.session_id), state)
        self.assertIsInstance(json.loads(self.store.read_text(encoding="utf-8")), dict)

    def test_concurrent_processes_preserve_all_sessions(self) -> None:
        script = """
import sys
import time
sys.path.insert(0, sys.argv[1])
import prewalk_core as core
time.sleep(0.15)
state = core.PrewalkState(session_id=sys.argv[3], preset=sys.argv[3])
core.save_state(sys.argv[2], state)
"""
        session_ids = [f"session-{index}" for index in range(20)]
        processes = [
            subprocess.Popen(
                [sys.executable, "-c", script, str(ROOT / "_shared"), str(self.store), session_id],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
            )
            for session_id in session_ids
        ]
        for process in processes:
            stdout, stderr = process.communicate(timeout=15)
            self.assertEqual(process.returncode, 0, stdout + stderr)

        data = json.loads(self.store.read_text(encoding="utf-8"))
        self.assertEqual(set(data), set(session_ids))
        for session_id in session_ids:
            self.assertEqual(core.load_state(self.store, session_id).preset, session_id)

    def test_vendored_core_copies_match_canonical_source(self) -> None:
        canonical = (ROOT / "_shared" / "prewalk_core.py").read_bytes()
        vendored = [
            ROOT / "codex" / "hooks" / "_shared" / "prewalk_core.py",
            ROOT / "claude-code" / "hooks" / "_shared" / "prewalk_core.py",
        ]

        for path in vendored:
            with self.subTest(path=path):
                self.assertEqual(path.read_bytes(), canonical)


if __name__ == "__main__":
    unittest.main()
