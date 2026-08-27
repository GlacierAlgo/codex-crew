from __future__ import annotations

import copy
import json
from pathlib import Path
import subprocess
import tempfile
import unittest

import codex_crew.lifecycle as lifecycle_module
from codex_crew.app_server import AppServerError
from codex_crew.lifecycle import (
    LifecycleError,
    build_lifecycle_record,
    close_crew,
    external_close_command,
    persist_new_lifecycle_record,
)


ENDPOINT = "unix:///tmp/lifecycle.sock"


class ArchiveState:
    def __init__(self) -> None:
        self.threads = {
            role: {
                "id": f"thread-{role}",
                "status": {"type": "idle"},
                "turns": [],
            }
            for role in ("commander", "worker", "judger")
        }
        self.archived: set[str] = set()
        self.archive_calls: list[str] = []
        self.fail_once_thread: str | None = None


class ArchiveConnection:
    def __init__(self, state: ArchiveState, endpoint: str) -> None:
        if endpoint != ENDPOINT:
            raise AppServerError(f"wrong endpoint {endpoint}")
        self.state = state

    def __enter__(self):
        return self

    def __exit__(self, *args) -> None:
        return None

    def request(self, method: str, params: dict | None = None):
        params = params or {}
        if method == "thread/list":
            return {
                "data": [{"id": thread_id} for thread_id in sorted(self.state.archived)],
                "nextCursor": None,
            }
        if method == "thread/read":
            role = params["threadId"].removeprefix("thread-")
            return {"thread": copy.deepcopy(self.state.threads[role])}
        if method == "thread/archive":
            thread_id = params["threadId"]
            self.state.archive_calls.append(thread_id)
            if self.state.fail_once_thread == thread_id:
                self.state.fail_once_thread = None
                raise AppServerError("fixture archive failure")
            self.state.archived.add(thread_id)
            return None
        raise AssertionError((method, params))


class TmuxRunner:
    def __init__(
        self,
        *,
        pane_ids: tuple[str, ...] = ("%10", "%11", "%12"),
        metadata: str = "default\t@7\tcrew-project\t6\n",
        display_error: str | None = None,
    ):
        self.pane_ids = pane_ids
        self.metadata = metadata
        self.display_error = display_error
        self.window_exists = True
        self.calls: list[tuple[str, ...]] = []

    def __call__(self, command) -> subprocess.CompletedProcess[str]:
        command = tuple(command)
        self.calls.append(command)
        operation = command[1]
        if operation == "display-message":
            if self.display_error is not None:
                return subprocess.CompletedProcess(
                    command, 1, "", self.display_error
                )
            if not self.window_exists:
                return subprocess.CompletedProcess(
                    command, 1, "", "can't find window: @7"
                )
            return subprocess.CompletedProcess(
                command, 0, self.metadata, ""
            )
        if operation == "list-panes":
            return subprocess.CompletedProcess(
                command, 0, "\n".join(self.pane_ids) + "\n", ""
            )
        if operation == "kill-window":
            self.assert_exact_target(command)
            self.window_exists = False
            return subprocess.CompletedProcess(command, 0, "", "")
        raise AssertionError(command)

    @staticmethod
    def assert_exact_target(command: tuple[str, ...]) -> None:
        if command != ("/tmux", "kill-window", "-t", "@7"):
            raise AssertionError(f"unexpected destructive tmux target: {command}")


class LifecycleTests(unittest.TestCase):
    def _persist(self, directory: Path) -> Path:
        record = build_lifecycle_record(
            loop_id="three-agent-dev",
            project_dir="/project",
            session="default",
            window_id="@7",
            window_name="crew-project",
            window_index="6",
            endpoint=ENDPOINT,
            communication_role="commander",
            roles=[
                {
                    "role": role,
                    "pane_id": f"%{10 + index}",
                    "thread_id": f"thread-{role}",
                    "bootstrap_turn_id": f"turn-{role}",
                }
                for index, role in enumerate(("commander", "worker", "judger"))
            ],
            handoff_turn_id="turn-handoff-commander",
            handoff_status="completed",
        )
        return persist_new_lifecycle_record(record, lifecycle_dir=directory)

    def test_close_reclaims_exact_window_and_archives_communication_last(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            lifecycle_dir = Path(directory) / "lifecycle"
            record_path = self._persist(lifecycle_dir)
            state = ArchiveState()
            tmux = TmuxRunner()
            result = close_crew(
                "@7",
                lifecycle_dir=lifecycle_dir,
                tmux_executable="/tmux",
                runner=tmux,
                connection_factory=lambda endpoint: ArchiveConnection(state, endpoint),
            )
            record_exists_after = record_path.exists()

        self.assertEqual(
            ["thread-worker", "thread-judger", "thread-commander"],
            state.archive_calls,
        )
        self.assertEqual(
            ("/tmux", "kill-window", "-t", "@7"), tmux.calls[2]
        )
        self.assertFalse(any(call[1] == "kill-session" for call in tmux.calls))
        self.assertFalse(record_exists_after)
        self.assertEqual("closed", result.as_dict()["status"])
        self.assertEqual(
            "codex-crew crew close --window-id @7", external_close_command("@7")
        )

    def test_active_preflight_has_zero_teardown_mutation(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            lifecycle_dir = Path(directory) / "lifecycle"
            record_path = self._persist(lifecycle_dir)
            state = ArchiveState()
            state.threads["worker"]["status"] = {"type": "active"}
            state.threads["worker"]["turns"] = [
                {"id": "active-worker", "status": "inProgress", "items": []}
            ]
            tmux = TmuxRunner()
            with self.assertRaisesRegex(
                LifecycleError,
                "role=worker thread=thread-worker turn=active-worker",
            ):
                close_crew(
                    "@7",
                    lifecycle_dir=lifecycle_dir,
                    tmux_executable="/tmux",
                    runner=tmux,
                    connection_factory=lambda endpoint: ArchiveConnection(
                        state, endpoint
                    ),
                )

            self.assertEqual([], tmux.calls)
            self.assertEqual([], state.archive_calls)
            self.assertTrue(record_path.is_file())

    def test_wrong_panes_and_unmanaged_or_traversal_record_fail_closed(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            lifecycle_dir = Path(directory) / "lifecycle"
            record_path = self._persist(lifecycle_dir)
            state = ArchiveState()
            tmux = TmuxRunner(pane_ids=("%10", "%11", "%99"))
            with self.assertRaisesRegex(LifecycleError, "pane set mismatch"):
                close_crew(
                    "@7",
                    lifecycle_dir=lifecycle_dir,
                    tmux_executable="/tmux",
                    runner=tmux,
                    connection_factory=lambda endpoint: ArchiveConnection(
                        state, endpoint
                    ),
                )
            self.assertFalse(any(call[1] == "kill-window" for call in tmux.calls))
            self.assertEqual([], state.archive_calls)
            self.assertTrue(record_path.is_file())

        with tempfile.TemporaryDirectory() as directory:
            lifecycle_dir = Path(directory) / "lifecycle"
            record_path = self._persist(lifecycle_dir)
            state = ArchiveState()
            tmux = TmuxRunner(metadata="other\t@7\tcrew-project\t6\n")
            with self.assertRaisesRegex(LifecycleError, "window metadata mismatch"):
                close_crew(
                    "@7",
                    lifecycle_dir=lifecycle_dir,
                    tmux_executable="/tmux",
                    runner=tmux,
                    connection_factory=lambda endpoint: ArchiveConnection(
                        state, endpoint
                    ),
                )
            self.assertFalse(any(call[1] == "kill-window" for call in tmux.calls))
            self.assertEqual([], state.archive_calls)
            self.assertTrue(record_path.is_file())

        with tempfile.TemporaryDirectory() as directory:
            lifecycle_dir = Path(directory) / "lifecycle"
            lifecycle_dir.mkdir()
            (lifecycle_dir / "window-7.json").write_text("{}\n", encoding="utf-8")
            with self.assertRaisesRegex(LifecycleError, "unmanaged lifecycle record"):
                close_crew("@7", lifecycle_dir=lifecycle_dir)
            with self.assertRaisesRegex(LifecycleError, "exact tmux ID"):
                close_crew("../../7", lifecycle_dir=lifecycle_dir)

    def test_partial_archive_checkpoints_and_retry_only_processes_remaining(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            lifecycle_dir = Path(directory) / "lifecycle"
            record_path = self._persist(lifecycle_dir)
            state = ArchiveState()
            state.fail_once_thread = "thread-judger"
            tmux = TmuxRunner()
            factory = lambda endpoint: ArchiveConnection(state, endpoint)
            with self.assertRaisesRegex(
                LifecycleError, "remaining roles: judger, commander"
            ):
                close_crew(
                    "@7",
                    lifecycle_dir=lifecycle_dir,
                    tmux_executable="/tmux",
                    runner=tmux,
                    connection_factory=factory,
                )
            checkpoint = json.loads(record_path.read_text(encoding="utf-8"))
            self.assertEqual(
                "complete",
                checkpoint["archive_progress"]["window_reclaim_phase"],
            )
            self.assertEqual(
                ["worker"], checkpoint["archive_progress"]["archived_roles"]
            )

            result = close_crew(
                "@7",
                lifecycle_dir=lifecycle_dir,
                tmux_executable="/tmux",
                runner=tmux,
                connection_factory=factory,
            )
            record_exists_after = record_path.exists()

        self.assertEqual(1, sum(call[1] == "kill-window" for call in tmux.calls))
        self.assertEqual(
            [
                "thread-worker",
                "thread-judger",
                "thread-judger",
                "thread-commander",
            ],
            state.archive_calls,
        )
        self.assertFalse(record_exists_after)
        self.assertEqual("closed", result.as_dict()["status"])

    def test_retry_reconciles_absent_window_after_complete_checkpoint_failure(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            lifecycle_dir = Path(directory) / "lifecycle"
            record_path = self._persist(lifecycle_dir)
            state = ArchiveState()
            tmux = TmuxRunner()
            factory = lambda endpoint: ArchiveConnection(state, endpoint)
            fail_complete_once = True

            def checkpoint(path, record):
                nonlocal fail_complete_once
                if record.window_reclaim_phase == "complete" and fail_complete_once:
                    fail_complete_once = False
                    raise LifecycleError("fixture complete checkpoint failure")
                lifecycle_module._checkpoint_record(path, record)

            with self.assertRaisesRegex(
                LifecycleError, "fixture complete checkpoint failure"
            ):
                close_crew(
                    "@7",
                    lifecycle_dir=lifecycle_dir,
                    tmux_executable="/tmux",
                    runner=tmux,
                    connection_factory=factory,
                    checkpoint=checkpoint,
                )
            started = json.loads(record_path.read_text(encoding="utf-8"))
            self.assertEqual(
                "started",
                started["archive_progress"]["window_reclaim_phase"],
            )
            self.assertEqual(1, sum(call[1] == "kill-window" for call in tmux.calls))
            self.assertEqual([], state.archive_calls)

            inspection_error = TmuxRunner(display_error="tmux server unavailable")
            with self.assertRaisesRegex(
                LifecycleError, "tmux server unavailable"
            ):
                close_crew(
                    "@7",
                    lifecycle_dir=lifecycle_dir,
                    tmux_executable="/tmux",
                    runner=inspection_error,
                    connection_factory=factory,
                )
            self.assertFalse(
                any(call[1] == "kill-window" for call in inspection_error.calls)
            )

            result = close_crew(
                "@7",
                lifecycle_dir=lifecycle_dir,
                tmux_executable="/tmux",
                runner=tmux,
                connection_factory=factory,
            )
            record_exists_after = record_path.exists()

        self.assertEqual(1, sum(call[1] == "kill-window" for call in tmux.calls))
        self.assertTrue(
            all(call == ("/tmux", "kill-window", "-t", "@7") for call in tmux.calls if call[1] == "kill-window")
        )
        self.assertEqual(
            ["thread-worker", "thread-judger", "thread-commander"],
            state.archive_calls,
        )
        self.assertFalse(record_exists_after)
        self.assertEqual("closed", result.as_dict()["status"])


if __name__ == "__main__":
    unittest.main()
