from __future__ import annotations

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
    def __init__(self, outcomes: dict[str, str] | None = None) -> None:
        self.outcomes = outcomes or {}
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
        raise AssertionError((method, params))


class FakeRunner:
    def __init__(
        self, state: DiscoveryState, *, fail_first_split: bool = False
    ) -> None:
        self.state = state
        self.calls: list[tuple[str, ...]] = []
        self.fail_first_split = fail_first_split
        self.horizontal_splits = 0

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
            self.horizontal_splits += 1
            if self.fail_first_split and self.horizontal_splits == 1:
                return _completed(command, returncode=1, stderr="no space")
            self.state.add_from_command(command[-1])
            return _completed(command, stdout=f"%{10 + self.horizontal_splits}\n")
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
            )

        self.assertEqual("even-horizontal", result.layout)
        self.assertEqual(
            {
                "commander": "thread-commander",
                "worker": "thread-worker",
                "judger": "thread-judger",
            },
            result.thread_mapping,
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

    def test_api_budget_launch_creates_four_committed_equal_width_roles(self) -> None:
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
            )

        self.assertEqual("api-budget-design", result.loop_id)
        self.assertEqual("even-horizontal", result.layout)
        self.assertEqual(
            {
                "designer_3": "thread-designer_3",
                "designer_4": "thread-designer_4",
                "designer_5": "thread-designer_5",
                "designer_6": "thread-designer_6",
            },
            result.thread_mapping,
        )
        self.assertEqual(
            {
                "designer_3": "%10",
                "designer_4": "%11",
                "designer_5": "%12",
                "designer_6": "%13",
            },
            result.pane_mapping,
        )
        self.assertEqual(4, len(result.panes))
        self.assertEqual({"high"}, {pane.reasoning_effort for pane in result.panes})
        self.assertEqual({"fast"}, {pane.service_tier for pane in result.panes})
        self.assertEqual(
            {"fast"},
            {pane["service_tier"] for pane in result.as_dict()["panes"]},
        )
        self.assertEqual(3, runner.horizontal_splits)
        self.assertEqual(
            1,
            sum(command[1] == "new-window" for command in runner.calls),
        )
        self.assertEqual(
            3,
            sum(command[1] == "split-window" for command in runner.calls),
        )
        for pane in result.panes:
            self.assertEqual(f"turn-{pane.role}", pane.bootstrap_turn_id)
            turn = state.threads[pane.thread_id]["turns"][0]
            self.assertEqual("completed", turn["status"])
            self.assertEqual(
                f"role={pane.role}", turn["items"][-1]["text"].splitlines()[0]
            )
        self.assertIn(
            ("/tmux", "select-layout", "-t", "@7", "even-horizontal"),
            runner.calls,
        )

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
                )
        self.assertFalse(
            any(command[1] in {"new-window", "split-window"} for command in runner.calls)
        )


if __name__ == "__main__":
    unittest.main()
