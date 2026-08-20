from __future__ import annotations

import json
import os
from pathlib import Path
import re
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
        extra_env: dict[str, str | None] | None = None,
        expected_returncode: int = 0,
    ) -> subprocess.CompletedProcess[str]:
        config = Path(self.temp_dir.name) / host
        config.mkdir(exist_ok=True)
        env = os.environ.copy()
        env["PYTHONDONTWRITEBYTECODE"] = "1"
        if host == "codex":
            env["CODEX_HOME"] = str(config)
            env.pop("CODEX_THREAD_ID", None)
            env.pop("CODEX_SESSION_ID", None)
        else:
            env["CLAUDE_CONFIG_DIR"] = str(config)
            env["CLAUDE_PLUGIN_ROOT"] = str(ROOT / "claude-code")
        for key, value in (extra_env or {}).items():
            if value is None:
                env.pop(key, None)
            else:
                env[key] = value
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
        self.assertEqual(result.returncode, expected_returncode, result.stdout + result.stderr)
        return result

    @staticmethod
    def handoff_token(output: str) -> str:
        match = re.search(r"PREWALK_HANDOFF_TOKEN: ([A-Za-z0-9_-]+)", output)
        if not match:
            raise AssertionError("handoff output did not contain a token")
        return match.group(1)

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
        self.assertIn("planner : active root session", armed.stdout)
        self.assertIn("configured: executor=gpt-5.6-terra", armed.stdout)
        self.assertIn("proven    : model=unproven", armed.stdout)
        self.assertNotIn("Switch this session", armed.stdout)

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
        self.assertIn('task_name="prewalk_executor_1"', handoff.stdout)
        self.assertIn('fork_turns="none"', handoff.stdout)
        self.assertIn('model="gpt-5.6-terra"', handoff.stdout)
        pending = self.run_script("codex", "_arm.py", "status", session_id)
        self.assertIn("handoff_requested", pending.stdout)

        confirmed = self.run_script("codex", "_pw.py", "confirm", session_id)
        self.assertIn("confirmed", confirmed.stdout)

        self.run_script(
            "codex",
            "todo_tracker.py",
            payload=self.todo_payload(session_id, "codex", completed=True),
        )
        status = self.run_script("codex", "_arm.py", "status", session_id)
        self.assertIn("idle", status.stdout)

    def test_codex_failed_handoff_is_retryable(self) -> None:
        session_id = "codex-retry"
        self.run_script("codex", "_arm.py", "arm", session_id, "Build the feature")
        todos = self.todo_payload(session_id, "codex")
        self.run_script("codex", "todo_tracker.py", payload=todos)
        self.run_script("codex", "edit_tracker.py", payload={
            "session_id": session_id,
            "tool_response": {"ok": True},
        })
        self.run_script("codex", "pause_detect.py", payload=dict(todos, hook_event_name="Stop"))
        self.run_script("codex", "_pw.py", "go", session_id)

        failed = self.run_script(
            "codex", "_pw.py", "fail", session_id, "spawn schema has no model"
        )
        self.assertIn("retryable", failed.stdout)
        status = self.run_script("codex", "_arm.py", "status", session_id)
        self.assertIn("paused", status.stdout)
        self.assertIn("spawn schema has no model", status.stdout)

    def test_codex_interleaved_threads_remain_isolated(self) -> None:
        sessions = ("thread-a", "thread-b")
        for session_id in sessions:
            env = {"CODEX_THREAD_ID": session_id, "CODEX_SESSION_ID": None}
            self.run_script(
                "codex", "_arm.py", "arm", session_id, "Build the feature", extra_env=env
            )
            todos = self.todo_payload(session_id, "codex")
            self.run_script("codex", "todo_tracker.py", payload=todos, extra_env=env)
            self.run_script("codex", "edit_tracker.py", payload={
                "session_id": session_id,
                "tool_response": {"ok": True},
            }, extra_env=env)
            self.run_script(
                "codex", "pause_detect.py",
                payload=dict(todos, hook_event_name="Stop"), extra_env=env,
            )
            self.run_script("codex", "_pw.py", "go", session_id, extra_env=env)

        self.run_script(
            "codex", "_pw.py", "fail", "thread-a", "route rejected",
            extra_env={"CODEX_THREAD_ID": "thread-a"},
        )
        self.run_script(
            "codex", "_pw.py", "confirm", "thread-b",
            extra_env={"CODEX_THREAD_ID": "thread-b"},
        )
        self.run_script(
            "codex", "todo_tracker.py",
            payload=self.todo_payload("thread-b", "codex", completed=True),
            extra_env={"CODEX_THREAD_ID": "thread-b"},
        )
        status_a = self.run_script(
            "codex", "_arm.py", "status", "thread-a",
            extra_env={"CODEX_THREAD_ID": "thread-a"},
        )
        status_b = self.run_script(
            "codex", "_arm.py", "status", "thread-b",
            extra_env={"CODEX_THREAD_ID": "thread-b"},
        )
        self.assertIn("paused", status_a.stdout)
        self.assertIn("route rejected", status_a.stdout)
        self.assertIn("idle", status_b.stdout)

        mismatch = self.run_script(
            "codex", "_arm.py", "status", "thread-b",
            extra_env={"CODEX_THREAD_ID": "thread-a"}, expected_returncode=1,
        )
        self.assertIn("conflicts", mismatch.stderr)
        self.assertIn(
            "paused",
            self.run_script(
                "codex", "_arm.py", "status", "thread-a",
                extra_env={"CODEX_THREAD_ID": "thread-a"},
            ).stdout,
        )

    def test_codex_missing_identity_does_not_create_state(self) -> None:
        result = self.run_script(
            "codex", "_arm.py", "arm", "", "Build the feature",
            extra_env={"CODEX_THREAD_ID": None, "CODEX_SESSION_ID": None},
            expected_returncode=1,
        )
        self.assertIn("cannot continue", result.stderr)
        store = Path(self.temp_dir.name) / "codex" / "prewalk-state.json"
        self.assertFalse(store.exists())

    def test_codex_doctor_reports_version_and_identity_remediation(self) -> None:
        result = self.run_script(
            "codex", "_arm.py", "doctor", "",
            extra_env={"CODEX_THREAD_ID": None, "CODEX_SESSION_ID": None},
            expected_returncode=1,
        )
        self.assertIn("Codex CLI >= 0.146.0", result.stdout)
        self.assertIn("thread identity", result.stdout)
        self.assertIn("upgrade Codex and restart the thread", result.stdout)

    def test_claude_scripts_route_and_complete_a_full_handoff(self) -> None:
        session_id = "claude-e2e"
        armed = self.run_script("claude-code", "_arm.py", "arm", session_id, "Build the feature")
        self.assertIn("prewalk ARMED", armed.stdout)
        self.assertIn("planner : active root session", armed.stdout)
        self.assertIn("configured: executor=haiku", armed.stdout)
        self.assertIn("proven    : model=hook-rewrite", armed.stdout)
        self.assertNotIn("Switch this session", armed.stdout)

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
        self.assertIn("spawn ONE Task", handoff.stdout)
        token = self.handoff_token(handoff.stdout)

        routed = self.run_script("claude-code", "handoff_router.py", payload={
            "session_id": session_id,
            "hook_event_name": "PreToolUse",
            "tool_use_id": "tool-prewalk",
            "tool_input": {
                "prompt": f"Finish the remaining plan\nPREWALK_HANDOFF_TOKEN: {token}",
                "subagent_type": "general-purpose",
            },
        })
        output = json.loads(routed.stdout)
        updated = output["hookSpecificOutput"]["updatedInput"]
        self.assertEqual(updated["model"], "haiku")
        self.assertEqual(updated["subagent_type"], "prewalk:prewalk-executor")
        pending = self.run_script("claude-code", "_arm.py", "status", session_id)
        self.assertIn("handoff_requested", pending.stdout)
        self.assertIn("routed: yes", pending.stdout)

        self.run_script("claude-code", "handoff_result.py", payload={
            "session_id": session_id,
            "hook_event_name": "PostToolUse",
            "tool_use_id": "tool-prewalk",
            "tool_response": {"success": True, "content": "background agent launched"},
        })
        acknowledged = self.run_script("claude-code", "_arm.py", "status", session_id)
        self.assertIn("handoff_requested", acknowledged.stdout)
        self.assertIn("launch_ack: yes", acknowledged.stdout)

        self.run_script("claude-code", "handoff_lifecycle.py", payload={
            "session_id": session_id,
            "hook_event_name": "SubagentStart",
            "agent_id": "agent-prewalk",
            "agent_type": "prewalk:prewalk-executor",
        })
        running = self.run_script("claude-code", "_arm.py", "status", session_id)
        self.assertIn("executor", running.stdout)
        self.assertIn("executor_agent: agent-prewalk", running.stdout)

        # Global plugin hooks also fire in subagents. Their todo updates must
        # not finish the root run before the bound SubagentStop marker arrives.
        executor_todos = self.todo_payload(session_id, "claude-code", completed=True)
        executor_todos.update({
            "agent_id": "agent-prewalk",
            "agent_type": "prewalk:prewalk-executor",
        })
        self.run_script("claude-code", "todo_tracker.py", payload=executor_todos)
        self.assertIn(
            "executor",
            self.run_script("claude-code", "_arm.py", "status", session_id).stdout,
        )

        # A different executor-shaped event cannot clear the bound run.
        self.run_script("claude-code", "handoff_lifecycle.py", payload={
            "session_id": session_id,
            "hook_event_name": "SubagentStop",
            "agent_id": "agent-unrelated",
            "agent_type": "prewalk:prewalk-executor",
            "last_assistant_message": "PREWALK_COMPLETE",
        })
        self.assertIn(
            "executor",
            self.run_script("claude-code", "_arm.py", "status", session_id).stdout,
        )

        self.run_script("claude-code", "handoff_lifecycle.py", payload={
            "session_id": session_id,
            "hook_event_name": "SubagentStop",
            "agent_id": "agent-prewalk",
            "agent_type": "prewalk:prewalk-executor",
            "last_assistant_message": "Done\nPREWALK_COMPLETE",
        })
        status = self.run_script("claude-code", "_arm.py", "status", session_id)
        self.assertIn("idle", status.stdout)

    def test_claude_conflicting_executor_override_refuses_to_arm(self) -> None:
        session_id = "claude-route-conflict"
        result = self.run_script(
            "claude-code",
            "_arm.py",
            "arm",
            session_id,
            "Build the feature",
            extra_env={"CLAUDE_CODE_SUBAGENT_MODEL": "sonnet"},
            expected_returncode=1,
        )

        self.assertIn("cannot arm", result.stderr)
        self.assertIn("override-conflict", result.stderr)
        store = Path(self.temp_dir.name) / "claude-code" / "prewalk-state.json"
        self.assertFalse(store.exists())

    def test_claude_failed_task_restores_retryable_checkpoint(self) -> None:
        session_id = "claude-retry"
        self.run_script("claude-code", "_arm.py", "arm", session_id, "Build the feature")
        self.run_script(
            "claude-code", "todo_tracker.py",
            payload=self.todo_payload(session_id, "claude-code"),
        )
        handoff = self.run_script("claude-code", "_pw.py", "go", session_id)
        token = self.handoff_token(handoff.stdout)
        self.run_script("claude-code", "handoff_router.py", payload={
            "session_id": session_id,
            "hook_event_name": "PreToolUse",
            "tool_use_id": "tool-failed",
            "tool_input": {
                "prompt": f"Finish the remaining plan\nPREWALK_HANDOFF_TOKEN: {token}"
            },
        })
        self.run_script("claude-code", "handoff_result.py", payload={
            "session_id": session_id,
            "hook_event_name": "PostToolUseFailure",
            "tool_use_id": "tool-failed",
            "error": "model unavailable",
        })
        status = self.run_script("claude-code", "_arm.py", "status", session_id)
        self.assertIn("paused", status.stdout)
        self.assertIn("model unavailable", status.stdout)

    def test_claude_missing_marker_and_duplicate_lifecycle_are_retryable(self) -> None:
        session_id = "claude-missing-marker"
        self.run_script("claude-code", "_arm.py", "arm", session_id, "Build the feature")
        self.run_script(
            "claude-code", "todo_tracker.py",
            payload=self.todo_payload(session_id, "claude-code"),
        )
        handoff = self.run_script("claude-code", "_pw.py", "go", session_id)
        token = self.handoff_token(handoff.stdout)
        self.run_script("claude-code", "handoff_router.py", payload={
            "session_id": session_id,
            "hook_event_name": "PreToolUse",
            "tool_use_id": "tool-missing",
            "tool_input": {"prompt": f"packet\nPREWALK_HANDOFF_TOKEN: {token}"},
        })
        start = {
            "session_id": session_id,
            "hook_event_name": "SubagentStart",
            "agent_id": "agent-missing",
            "agent_type": "prewalk:prewalk-executor",
        }
        self.run_script("claude-code", "handoff_lifecycle.py", payload=start)
        self.run_script("claude-code", "handoff_lifecycle.py", payload=start)
        self.run_script("claude-code", "handoff_lifecycle.py", payload={
            **start,
            "hook_event_name": "SubagentStop",
            "last_assistant_message": "Finished but omitted the marker",
        })
        status = self.run_script("claude-code", "_arm.py", "status", session_id)
        self.assertIn("paused", status.stdout)
        self.assertIn("without a PREWALK completion marker", status.stdout)

    def test_claude_unrelated_agent_events_leave_pending_handoff_unchanged(self) -> None:
        session_id = "claude-unrelated"
        self.run_script("claude-code", "_arm.py", "arm", session_id, "Build the feature")
        self.run_script(
            "claude-code", "todo_tracker.py",
            payload=self.todo_payload(session_id, "claude-code"),
        )
        handoff = self.run_script("claude-code", "_pw.py", "go", session_id)
        token = self.handoff_token(handoff.stdout)
        unrelated = self.run_script("claude-code", "handoff_router.py", payload={
            "session_id": session_id,
            "hook_event_name": "PreToolUse",
            "tool_use_id": "tool-unrelated",
            "tool_input": {"prompt": "unrelated Agent request"},
        })
        self.assertEqual(unrelated.stdout, "")
        self.run_script("claude-code", "handoff_result.py", payload={
            "session_id": session_id,
            "hook_event_name": "PostToolUse",
            "tool_use_id": "tool-unrelated",
            "tool_response": {"success": True, "content": "PREWALK_COMPLETE"},
        })
        status = self.run_script("claude-code", "_arm.py", "status", session_id)
        self.assertIn("handoff_requested", status.stdout)
        self.assertIn("routed: no", status.stdout)

        nested = self.run_script("claude-code", "handoff_router.py", payload={
            "session_id": session_id,
            "hook_event_name": "PreToolUse",
            "tool_use_id": "tool-nested",
            "agent_id": "parent-agent",
            "tool_input": {"prompt": f"packet\nPREWALK_HANDOFF_TOKEN: {token}"},
        })
        self.assertEqual(nested.stdout, "")
        self.assertIn(
            "routed: no",
            self.run_script("claude-code", "_arm.py", "status", session_id).stdout,
        )

    def test_claude_foreground_lifecycle_completes_before_agent_posttooluse(self) -> None:
        session_id = "claude-foreground"
        self.run_script("claude-code", "_arm.py", "arm", session_id, "Build the feature")
        self.run_script(
            "claude-code", "todo_tracker.py",
            payload=self.todo_payload(session_id, "claude-code"),
        )
        handoff = self.run_script("claude-code", "_pw.py", "go", session_id)
        token = self.handoff_token(handoff.stdout)
        self.run_script("claude-code", "handoff_router.py", payload={
            "session_id": session_id,
            "hook_event_name": "PreToolUse",
            "tool_use_id": "tool-foreground",
            "tool_input": {"prompt": f"packet\nPREWALK_HANDOFF_TOKEN: {token}"},
        })
        lifecycle = {
            "session_id": session_id,
            "agent_id": "agent-foreground",
            "agent_type": "prewalk:prewalk-executor",
        }
        self.run_script(
            "claude-code", "handoff_lifecycle.py",
            payload={**lifecycle, "hook_event_name": "SubagentStart"},
        )
        self.run_script(
            "claude-code", "handoff_lifecycle.py",
            payload={
                **lifecycle,
                "hook_event_name": "SubagentStop",
                "last_assistant_message": "PREWALK_COMPLETE",
            },
        )
        # Foreground Agent PostToolUse arrives after SubagentStop and is harmless.
        self.run_script("claude-code", "handoff_result.py", payload={
            "session_id": session_id,
            "hook_event_name": "PostToolUse",
            "tool_use_id": "tool-foreground",
            "tool_response": {"success": True},
        })
        self.assertIn(
            "idle",
            self.run_script("claude-code", "_arm.py", "status", session_id).stdout,
        )

    def test_claude_token_call_without_tool_identity_is_denied_and_retryable(self) -> None:
        session_id = "claude-no-tool-id"
        self.run_script("claude-code", "_arm.py", "arm", session_id, "Build the feature")
        self.run_script(
            "claude-code", "todo_tracker.py",
            payload=self.todo_payload(session_id, "claude-code"),
        )
        handoff = self.run_script("claude-code", "_pw.py", "go", session_id)
        token = self.handoff_token(handoff.stdout)
        denied = self.run_script("claude-code", "handoff_router.py", payload={
            "session_id": session_id,
            "hook_event_name": "PreToolUse",
            "tool_input": {"prompt": f"packet\nPREWALK_HANDOFF_TOKEN: {token}"},
        })
        output = json.loads(denied.stdout)
        self.assertEqual(output["hookSpecificOutput"]["permissionDecision"], "deny")
        status = self.run_script("claude-code", "_arm.py", "status", session_id)
        self.assertIn("paused", status.stdout)
        self.assertIn("tool_use_id", status.stdout)

    def test_claude_fast_mode_requests_handoff_from_stop_hook(self) -> None:
        session_id = "claude-fast"
        self.run_script(
            "claude-code", "_arm.py", "arm", session_id, "--fast Build the feature"
        )
        self.run_script(
            "claude-code", "todo_tracker.py",
            payload=self.todo_payload(session_id, "claude-code"),
        )
        stopped = self.run_script("claude-code", "pause_detect.py", payload={
            "session_id": session_id,
            "hook_event_name": "Stop",
        })
        output = json.loads(stopped.stdout)
        self.assertEqual(output["decision"], "block")
        self.assertIn("spawn ONE Task", output["reason"])
        status = self.run_script("claude-code", "_arm.py", "status", session_id)
        self.assertIn("handoff_requested", status.stdout)


if __name__ == "__main__":
    unittest.main()
