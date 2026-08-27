from __future__ import annotations

import json
from pathlib import Path
import shlex
import subprocess
import tempfile
import unittest

from codex_crew.app_server import AppServerError
from codex_crew.launcher import (
    DEFAULT_DISCOVERY_TIMEOUT_SECONDS,
    LaunchError,
    launch_crew,
)


ENDPOINT = "unix:///tmp/native.sock"


def _completed(
    command,
    *,
    returncode: int = 0,
    stdout: str = "",
    stderr: str = "",
) -> subprocess.CompletedProcess[str]:
    return subprocess.CompletedProcess(command, returncode, stdout, stderr)


class DiscoveryState:
    def __init__(
        self,
        outcomes: dict[str, str] | None = None,
        *,
        handoff_outcome: str = "completed",
    ) -> None:
        self.outcomes = outcomes or {}
        self.handoff_outcome = handoff_outcome
        self.handoff_messages: list[tuple[str, str]] = []
        self.threads: dict[str, dict] = {
            "existing-thread": {
                "id": "existing-thread",
                "source_kind": "cli",
                "status": {"type": "idle"},
                "turns": [],
            }
        }
        self.list_params: list[dict] = []

    def add_from_command(self, startup: str) -> None:
        arguments = shlex.split(startup)
        prompt = arguments[-1]
        role_line = next(
            line for line in prompt.splitlines() if line.startswith("role=")
        )
        role = role_line.removeprefix("role=")
        marker = next(
            line
            for line in prompt.splitlines()
            if line.startswith("CODEX_CREW_BOOTSTRAP:")
        )
        outcome = self.outcomes.get(role, "completed")
        items = [
            {
                "type": "userMessage",
                "content": [
                    {
                        "type": "text",
                        "text": f"{marker}\n{role_line}\nbootstrap",
                    }
                ],
            }
        ]
        if outcome == "completed":
            items.append(
                {
                    "type": "agentMessage",
                    "phase": "final_answer",
                    "text": f"role={role}\n职责确认。",
                }
            )
        elif outcome == "wrong":
            items.append(
                {
                    "type": "agentMessage",
                    "phase": "final_answer",
                    "text": "role=wrong\n职责错误。",
                }
            )
        elif outcome not in {"missing", "failed", "interrupted", "inProgress"}:
            raise AssertionError(f"unknown bootstrap outcome {outcome!r}")
        status = "completed" if outcome in {"completed", "wrong", "missing"} else outcome
        thread_id = f"thread-{role}"
        self.threads[thread_id] = {
            "id": thread_id,
            # Live remote TUI evidence reports vscode for these threads.
            "source_kind": "vscode",
            "status": {
                "type": "active" if status == "inProgress" else "idle"
            },
            "turns": [
                {
                    "id": f"turn-{role}",
                    "status": status,
                    "items": items,
                }
            ],
        }


class FakeConnection:
    def __init__(self, state: DiscoveryState, endpoint: str) -> None:
        if endpoint != ENDPOINT:
            raise AppServerError("wrong endpoint")
        self.state = state

    def __enter__(self):
        return self

    def __exit__(self, *args) -> None:
        return None

    def request(self, method: str, params: dict | None = None):
        if method == "thread/list":
            assert params is not None
            self.state.list_params.append(dict(params))
            source_kinds = set(params.get("sourceKinds", []))
            return {
                "data": [
                    {"id": thread_id}
                    for thread_id, thread in reversed(tuple(self.state.threads.items()))
                    if thread["source_kind"] in source_kinds
                ],
                "nextCursor": None,
            }
        if method == "thread/read":
            assert params is not None
            thread = dict(self.state.threads[params["threadId"]])
            thread.pop("source_kind")
            return {"thread": thread}
        if method == "turn/start":
            assert params is not None
            thread_id = params["threadId"]
            message = params["input"][0]["text"]
            role = thread_id.removeprefix("thread-")
            self.state.handoff_messages.append((thread_id, message))
            status = (
                "completed"
                if self.state.handoff_outcome
                in {
                    "completed",
                    "missing_final",
                    "wrong_readiness",
                    "missing_readiness",
                }
                else self.state.handoff_outcome
            )
            items = [
                {
                    "type": "userMessage",
                    "content": [{"type": "text", "text": message}],
                }
            ]
            if self.state.handoff_outcome in {
                "completed",
                "wrong_readiness",
                "missing_readiness",
            }:
                final_text = {
                    "completed": "runtime_handoff=ready\n控制映射已保存。",
                    "wrong_readiness": (
                        "prefix runtime_handoff=ready\n错误的 substring acknowledgement。"
                    ),
                    "missing_readiness": "控制映射已保存，但未声明 readiness。",
                }[self.state.handoff_outcome]
                items.append(
                    {
                        "type": "agentMessage",
                        "phase": "final_answer",
                        "text": final_text,
                    }
                )
            turn = {
                "id": f"turn-handoff-{role}",
                "status": status,
                "items": items,
            }
            self.state.threads[thread_id]["turns"].append(turn)
            self.state.threads[thread_id]["status"] = {"type": "idle"}
            return {"turn": turn}
        raise AssertionError((method, params))


class FakeRunner:
    def __init__(
        self, state: DiscoveryState, *, fail_first_split: bool = False
    ) -> None:
        self.state = state
        self.calls: list[tuple[str, ...]] = []
        self.fail_first_split = fail_first_split
        self.split_count = 0

    def __call__(self, command) -> subprocess.CompletedProcess[str]:
        command = tuple(command)
        self.calls.append(command)
        if command[0] == "/codex":
            return _completed(command, stdout="codex-cli test\n")
        operation = command[1]
        if operation == "has-session":
            return _completed(command)
        if operation == "new-window":
            self.state.add_from_command(command[-1])
            return _completed(command, stdout="@7\t%10\t6\n")
        if operation == "split-window":
            self.split_count += 1
            if self.fail_first_split and self.split_count == 1:
                return _completed(command, returncode=1, stderr="no space")
            self.state.add_from_command(command[-1])
            return _completed(command, stdout=f"%{10 + self.split_count}\n")
        if operation in {"select-layout", "kill-window"}:
            return _completed(command)
        raise AssertionError(f"unexpected command: {command}")


class Clock:
    def __init__(self) -> None:
        self.now = 0.0

    def monotonic(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.now += seconds


class LauncherTests(unittest.TestCase):
    def test_launch_requires_committed_identity_and_covers_interactive_sources(self) -> None:
        state = DiscoveryState()
        runner = FakeRunner(state)
        with tempfile.TemporaryDirectory() as directory:
            project = Path(directory)
            (project / "README.md").write_text("# Product\n", encoding="utf-8")
            result = launch_crew(
                project,
                app_server_endpoint=ENDPOINT,
                tmux_executable="/tmux",
                codex_executable="/codex",
                runner=runner,
                connection_factory=lambda endpoint: FakeConnection(state, endpoint),
                marker_factory=lambda: "launch-one",
                lifecycle_dir=project / "lifecycle",
            )
            lifecycle_record = json.loads(
                Path(result.lifecycle_record_path).read_text(encoding="utf-8")
            )

        self.assertEqual("even-horizontal", result.layout)
        self.assertEqual(
            f"crew-three-agent-dev-{project.name}", result.window_name
        )
        self.assertEqual(
            {
                "commander": "thread-commander",
                "worker": "thread-worker",
                "judger": "thread-judger",
            },
            result.thread_mapping,
        )
        self.assertEqual("commander", result.communication_role)
        self.assertEqual("thread-commander", result.communication_thread_id)
        self.assertEqual("%10", result.communication_pane_id)
        self.assertEqual("turn-handoff-commander", result.handoff_turn_id)
        self.assertEqual("completed", result.handoff_status)
        self.assertEqual(
            "codex-crew crew close --window-id @7", result.close_command
        )
        self.assertEqual("@7", lifecycle_record["window"]["id"])
        self.assertEqual(
            "turn-handoff-commander", lifecycle_record["handoff"]["turn_id"]
        )
        self.assertEqual(
            "pending",
            lifecycle_record["archive_progress"]["window_reclaim_phase"],
        )
        self.assertNotIn("discovery_complete", result.as_dict())
        self.assertNotIn("discovery_error", result.as_dict())
        self.assertEqual({"high"}, {pane.reasoning_effort for pane in result.panes})
        self.assertEqual({"fast"}, {pane.service_tier for pane in result.panes})
        self.assertEqual(
            {"fast"},
            {pane["service_tier"] for pane in result.as_dict()["panes"]},
        )
        self.assertTrue(state.list_params)
        for params in state.list_params:
            self.assertEqual({"cli", "vscode"}, set(params["sourceKinds"]))
            self.assertEqual(result.project_dir, params["cwd"])

        startup_commands = [
            command[-1]
            for command in runner.calls
            if command[1] in {"new-window", "split-window"}
        ]
        self.assertEqual(3, len(startup_commands))
        for startup, pane in zip(startup_commands, result.panes, strict=True):
            arguments = shlex.split(startup)
            self.assertEqual("/codex", arguments[0])
            self.assertEqual(pane.runtime_profile, arguments[2])
            self.assertIn("--strict-config", arguments)
            self.assertIn("--yolo", arguments)
            self.assertEqual(ENDPOINT, arguments[arguments.index("--remote") + 1])
            self.assertEqual(result.project_dir, arguments[arguments.index("-C") + 1])
            self.assertIn(pane.bootstrap_marker, arguments[-1].splitlines())
            self.assertIn(f"role={pane.role}", arguments[-1].splitlines())
            self.assertNotIn("resume", arguments)
            turn = state.threads[pane.thread_id]["turns"][0]
            self.assertEqual("completed", turn["status"])
            self.assertEqual(
                f"role={pane.role}", turn["items"][-1]["text"].splitlines()[0]
            )

        self.assertIn(
            ("/tmux", "select-layout", "-t", "@7", "even-horizontal"),
            runner.calls,
        )
        self.assertFalse(any(command[1] == "set-option" for command in runner.calls))
        self.assertEqual(1, len(state.handoff_messages))
        handoff_thread, handoff_message = state.handoff_messages[0]
        self.assertEqual("thread-commander", handoff_thread)
        self.assertTrue(handoff_message.startswith("CODEX_CREW_RUNTIME_HANDOFF\n"))
        for expected in (
            '"loop_id": "three-agent-dev"',
            '"project_dir":',
            '"session": "default"',
            '"id": "@7"',
            '"index": "6"',
            '"name": "crew-three-agent-dev-',
            f'"endpoint": "{ENDPOINT}"',
            '"communication_role": "commander"',
            '"external_close_command": "codex-crew crew close --window-id @7"',
            '"pane_id": "%10"',
            '"thread_id": "thread-worker"',
            '"bootstrap_turn_id": "turn-judger"',
        ):
            self.assertIn(expected, handoff_message)

    def test_api_budget_launch_executes_manifest_split_plan(self) -> None:
        state = DiscoveryState()
        runner = FakeRunner(state)
        with tempfile.TemporaryDirectory() as directory:
            project = Path(directory)
            (project / "README.md").write_text("# Product\n", encoding="utf-8")
            result = launch_crew(
                project,
                loop_id="api-budget-design",
                app_server_endpoint=ENDPOINT,
                tmux_executable="/tmux",
                codex_executable="/codex",
                runner=runner,
                connection_factory=lambda endpoint: FakeConnection(state, endpoint),
                marker_factory=lambda: "launch-api-budget",
                lifecycle_dir=project / "lifecycle",
            )

        self.assertEqual("api-budget-design", result.loop_id)
        self.assertEqual(
            f"crew-api-budget-design-{project.name}", result.window_name
        )
        self.assertEqual("split-plan", result.layout)
        self.assertEqual(
            {
                "commander": "thread-commander",
                "worker_3": "thread-worker_3",
                "worker_4": "thread-worker_4",
                "worker_5": "thread-worker_5",
                "worker_6": "thread-worker_6",
            },
            result.thread_mapping,
        )
        self.assertEqual(
            {
                "commander": "%10",
                "worker_3": "%11",
                "worker_4": "%12",
                "worker_5": "%13",
                "worker_6": "%14",
            },
            result.pane_mapping,
        )
        self.assertEqual(5, len(result.panes))
        self.assertEqual("commander", result.communication_role)
        self.assertEqual("thread-commander", result.communication_thread_id)
        self.assertEqual("%10", result.communication_pane_id)
        self.assertEqual("turn-handoff-commander", result.handoff_turn_id)
        self.assertEqual({"high"}, {pane.reasoning_effort for pane in result.panes})
        self.assertEqual({"fast"}, {pane.service_tier for pane in result.panes})
        self.assertEqual(
            {"fast"},
            {pane["service_tier"] for pane in result.as_dict()["panes"]},
        )
        self.assertEqual(4, runner.split_count)
        self.assertEqual(
            1,
            sum(command[1] == "new-window" for command in runner.calls),
        )
        self.assertEqual(
            4,
            sum(command[1] == "split-window" for command in runner.calls),
        )
        for pane in result.panes:
            self.assertEqual(f"turn-{pane.role}", pane.bootstrap_turn_id)
            turn = state.threads[pane.thread_id]["turns"][0]
            self.assertEqual("completed", turn["status"])
            self.assertEqual(
                f"role={pane.role}", turn["items"][-1]["text"].splitlines()[0]
            )
        split_commands = [
            command for command in runner.calls if command[1] == "split-window"
        ]
        self.assertEqual(
            [
                ("-h", "67", "%10"),
                ("-v", "50", "%11"),
                ("-h", "50", "%11"),
                ("-h", "50", "%12"),
            ],
            [
                (
                    command[2],
                    command[command.index("-p") + 1],
                    command[command.index("-t") + 1],
                )
                for command in split_commands
            ],
        )
        for command in split_commands:
            self.assertEqual(
                ("-d", "-P", "-F", "#{pane_id}"), command[5:9]
            )
        self.assertFalse(
            any(command[1] == "select-layout" for command in runner.calls)
        )

    def test_communication_handoff_failure_is_nonzero_and_preserves_window(self) -> None:
        cases = {
            "failed": "status 'failed'",
            "wrong_readiness": "declared first line 'prefix runtime_handoff=ready'",
            "missing_readiness": (
                "declared first line '控制映射已保存，但未声明 readiness。'"
            ),
        }
        for outcome, expected in cases.items():
            with self.subTest(outcome=outcome):
                state = DiscoveryState(handoff_outcome=outcome)
                runner = FakeRunner(state)
                with tempfile.TemporaryDirectory() as directory:
                    project = Path(directory)
                    (project / "README.md").write_text(
                        "# Product\n", encoding="utf-8"
                    )
                    with self.assertRaises(LaunchError) as caught:
                        launch_crew(
                            project,
                            app_server_endpoint=ENDPOINT,
                            tmux_executable="/tmux",
                            codex_executable="/codex",
                            runner=runner,
                            connection_factory=lambda endpoint: FakeConnection(
                                state, endpoint
                            ),
                            marker_factory=lambda: f"launch-handoff-{outcome}",
                            lifecycle_dir=project / "lifecycle",
                        )

                message = str(caught.exception)
                self.assertIn("crew window @7 communication handoff failed", message)
                self.assertIn("role 'commander'", message)
                self.assertIn(expected, message)
                self.assertIn("window preserved for diagnosis", message)
                self.assertNotIn(
                    ("/tmux", "kill-window", "-t", "@7"), runner.calls
                )

    def test_lifecycle_persist_failure_is_nonzero_and_preserves_window(self) -> None:
        state = DiscoveryState()
        runner = FakeRunner(state)
        with tempfile.TemporaryDirectory() as directory:
            project = Path(directory)
            (project / "README.md").write_text("# Product\n", encoding="utf-8")
            lifecycle_path = project / "lifecycle"
            lifecycle_path.write_text("conflict\n", encoding="utf-8")
            with self.assertRaises(LaunchError) as caught:
                launch_crew(
                    project,
                    app_server_endpoint=ENDPOINT,
                    tmux_executable="/tmux",
                    codex_executable="/codex",
                    runner=runner,
                    connection_factory=lambda endpoint: FakeConnection(state, endpoint),
                    marker_factory=lambda: "launch-lifecycle-failure",
                    lifecycle_dir=lifecycle_path,
                )

        message = str(caught.exception)
        self.assertIn("crew window @7 lifecycle record persist failed", message)
        self.assertIn("window preserved for diagnosis", message)
        self.assertEqual(1, len(state.handoff_messages))
        self.assertNotIn(("/tmux", "kill-window", "-t", "@7"), runner.calls)

    def test_discovery_deadline_is_nonzero_failure_and_preserves_window(self) -> None:
        self.assertEqual(120.0, DEFAULT_DISCOVERY_TIMEOUT_SECONDS)
        state = DiscoveryState({"judger": "inProgress"})
        runner = FakeRunner(state)
        clock = Clock()
        with tempfile.TemporaryDirectory() as directory:
            project = Path(directory)
            (project / "README.md").write_text("# Product\n", encoding="utf-8")
            with self.assertRaises(LaunchError) as caught:
                launch_crew(
                    project,
                    app_server_endpoint=ENDPOINT,
                    tmux_executable="/tmux",
                    codex_executable="/codex",
                    runner=runner,
                    connection_factory=lambda endpoint: FakeConnection(state, endpoint),
                    discovery_timeout_seconds=0.2,
                    discovery_poll_seconds=0.1,
                    sleep=clock.sleep,
                    monotonic=clock.monotonic,
                    marker_factory=lambda: "launch-timeout",
                    lifecycle_dir=project / "lifecycle",
                )
        message = str(caught.exception)
        self.assertIn("crew window @7", message)
        self.assertIn("missing roles: judger", message)
        self.assertIn("window preserved for diagnosis", message)
        self.assertNotIn(("/tmux", "kill-window", "-t", "@7"), runner.calls)

    def test_terminal_bootstrap_failures_fail_closed_and_preserve_window(self) -> None:
        cases = {
            "failed": "status 'failed'",
            "interrupted": "status 'interrupted'",
            "wrong": "expected 'role=worker'",
            "missing": "no authoritative final_answer agentMessage",
        }
        for outcome, expected in cases.items():
            with self.subTest(outcome=outcome):
                state = DiscoveryState({"worker": outcome})
                runner = FakeRunner(state)
                with tempfile.TemporaryDirectory() as directory:
                    project = Path(directory)
                    (project / "README.md").write_text(
                        "# Product\n", encoding="utf-8"
                    )
                    with self.assertRaises(LaunchError) as caught:
                        launch_crew(
                            project,
                            app_server_endpoint=ENDPOINT,
                            tmux_executable="/tmux",
                            codex_executable="/codex",
                            runner=runner,
                            connection_factory=lambda endpoint: FakeConnection(
                                state, endpoint
                            ),
                            marker_factory=lambda: f"launch-{outcome}",
                            lifecycle_dir=project / "lifecycle",
                        )
                message = str(caught.exception)
                self.assertIn("crew window @7", message)
                self.assertIn("role 'worker'", message)
                self.assertIn(expected, message)
                self.assertIn("window preserved for diagnosis", message)
                self.assertNotIn(
                    ("/tmux", "kill-window", "-t", "@7"), runner.calls
                )

    def test_tmux_failure_kills_only_the_created_window(self) -> None:
        state = DiscoveryState()
        runner = FakeRunner(state, fail_first_split=True)
        with tempfile.TemporaryDirectory() as directory:
            project = Path(directory)
            (project / "README.md").write_text("# Product\n", encoding="utf-8")
            with self.assertRaisesRegex(LaunchError, "failed to create the worker pane"):
                launch_crew(
                    project,
                    app_server_endpoint=ENDPOINT,
                    tmux_executable="/tmux",
                    codex_executable="/codex",
                    runner=runner,
                    connection_factory=lambda endpoint: FakeConnection(state, endpoint),
                    marker_factory=lambda: "launch-two",
                    lifecycle_dir=project / "lifecycle",
                )
        self.assertIn(("/tmux", "kill-window", "-t", "@7"), runner.calls)

    def test_app_server_preflight_failure_does_not_mutate_tmux(self) -> None:
        state = DiscoveryState()
        runner = FakeRunner(state)
        with tempfile.TemporaryDirectory() as directory:
            project = Path(directory)
            (project / "README.md").write_text("# Product\n", encoding="utf-8")

            def unavailable(endpoint: str):
                raise AppServerError("socket unavailable")

            with self.assertRaisesRegex(LaunchError, "discovery preflight"):
                launch_crew(
                    project,
                    app_server_endpoint=ENDPOINT,
                    tmux_executable="/tmux",
                    codex_executable="/codex",
                    runner=runner,
                    connection_factory=unavailable,
                    lifecycle_dir=project / "lifecycle",
                )
        self.assertFalse(
            any(command[1] in {"new-window", "split-window"} for command in runner.calls)
        )


if __name__ == "__main__":
    unittest.main()
