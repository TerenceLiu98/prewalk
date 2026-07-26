from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[1]


class EndToEndFlowTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp_dir.cleanup)

    def run_script(
        self,
        host: str,
        script: str,
        *arguments: str,
        payload: dict | None = None,
    ) -> subprocess.CompletedProcess[str]:
        config = Path(self.temp_dir.name) / host
        config.mkdir(exist_ok=True)
        env = os.environ.copy()
        env["PYTHONDONTWRITEBYTECODE"] = "1"
        if host == "codex":
            env["CODEX_HOME"] = str(config)
        else:
            env["CLAUDE_CONFIG_DIR"] = str(config)
            env["CLAUDE_PLUGIN_ROOT"] = str(ROOT / "claude-code")
        result = subprocess.run(
            [sys.executable, str(ROOT / host / "hooks" / script), *arguments],
            input=json.dumps(payload) if payload is not None else None,
            text=True,
            capture_output=True,
            env=env,
            cwd=ROOT,
            timeout=10,
            check=False,
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        return result

    @staticmethod
    def todo_payload(session_id: str, host: str, *, completed: bool = False) -> dict:
        status = "completed" if completed else "pending"
        items = [
            {"content": "Implement core and test", "status": "completed"},
            {"content": "Update adapters and verify", "status": status},
            {"content": "Update docs and check", "status": status},
            {"content": "PAUSE for handoff", "status": "completed" if completed else "in_progress"},
        ]
        key = "plan" if host == "codex" else "todos"
        return {
            "session_id": session_id,
            "hook_event_name": "PostToolUse",
            "tool_input": {key: items},
            "tool_response": {"success": True},
        }

    def test_codex_scripts_complete_a_full_handoff(self) -> None:
        session_id = "codex-e2e"
        armed = self.run_script("codex", "_arm.py", "arm", session_id, "Build the feature")
        self.assertIn("prewalk ARMED", armed.stdout)

        todos = self.todo_payload(session_id, "codex")
        self.run_script("codex", "todo_tracker.py", payload=todos)
        self.run_script("codex", "edit_tracker.py", payload={
            "session_id": session_id,
            "tool_response": {"ok": True, "executed": True},
        })
        checkpoint = dict(todos, hook_event_name="Stop")
        paused = self.run_script("codex", "pause_detect.py", payload=checkpoint)
        self.assertIn("PAUSE", paused.stdout)

        handoff = self.run_script("codex", "_pw.py", "go", session_id)
        self.assertIn("spawn_agent", handoff.stdout)
        self.assertIn("fork_context", handoff.stdout)

        self.run_script(
            "codex",
            "todo_tracker.py",
            payload=self.todo_payload(session_id, "codex", completed=True),
        )
        status = self.run_script("codex", "_arm.py", "status", session_id)
        self.assertIn("idle", status.stdout)

    def test_claude_scripts_route_and_complete_a_full_handoff(self) -> None:
        session_id = "claude-e2e"
        armed = self.run_script("claude-code", "_arm.py", "arm", session_id, "Build the feature")
        self.assertIn("prewalk ARMED", armed.stdout)

        self.run_script(
            "claude-code",
            "todo_tracker.py",
            payload=self.todo_payload(session_id, "claude-code"),
        )
        self.run_script("claude-code", "edit_tracker.py", payload={
            "session_id": session_id,
            "tool_response": {"filePath": "/tmp/example", "success": True},
        })
        handoff = self.run_script("claude-code", "_pw.py", "go", session_id)
        self.assertIn("spawning ONE Task", handoff.stdout)

        routed = self.run_script("claude-code", "handoff_router.py", payload={
            "session_id": session_id,
            "tool_input": {"prompt": "Finish the remaining plan", "subagent_type": "general-purpose"},
        })
        output = json.loads(routed.stdout)
        updated = output["hookSpecificOutput"]["updatedInput"]
        self.assertEqual(updated["model"], "haiku")
        self.assertEqual(updated["subagent_type"], "prewalk:prewalk-executor")

        self.run_script(
            "claude-code",
            "todo_tracker.py",
            payload=self.todo_payload(session_id, "claude-code", completed=True),
        )
        status = self.run_script("claude-code", "_arm.py", "status", session_id)
        self.assertIn("idle", status.stdout)


if __name__ == "__main__":
    unittest.main()
