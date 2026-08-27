from __future__ import annotations

import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from click.testing import CliRunner

from codex_crew.app_server import AppServerEndpoint
from codex_crew.cli import cli
from codex_crew.crew_runtime import CrewCommandResult
from codex_crew.launcher import CrewLaunch, CrewPane, LaunchError
from codex_crew.startup import StartupError


def _launch_result(endpoint: str = "unix://") -> CrewLaunch:
    roles = ("commander", "worker", "judger")
    return CrewLaunch(
        loop_id="three-agent-dev",
        layout="even-horizontal",
        app_server_endpoint=endpoint,
        session="default",
        window_id="@7",
        window_index="6",
        window_name="crew-project",
        project_dir="/project",
        panes=tuple(
            CrewPane(
                role=role,
                pane_id=f"%{10 + index}",
                runtime_profile=f"codex-crew-three-agent-dev-{role}",
                model="gpt-5.6-sol",
                reasoning_effort="xhigh",
                bootstrap_marker=f"marker-{role}",
                thread_id=f"thread-{role}",
                bootstrap_turn_id=f"turn-{role}",
            )
            for index, role in enumerate(roles)
        ),
    )


class ClickCliTests(unittest.TestCase):
    @patch("codex_crew.cli.run_stop_hook", return_value=0)
    def test_hook_cli_keeps_independent_snapshot_database(self, hook_mock) -> None:
        with tempfile.TemporaryDirectory() as directory:
            database = Path(directory) / "snapshots.sqlite3"
            result = CliRunner().invoke(
                cli,
                ["--db", str(database), "hook", "stop"],
                input="{}",
            )

        self.assertEqual(0, result.exit_code, result.output)
        hook_mock.assert_called_once_with(database_path=database)

    def test_init_db_latest_and_final_keep_direct_snapshot_contract(self) -> None:
        runner = CliRunner()
        with tempfile.TemporaryDirectory() as directory:
            database = Path(directory) / "snapshots.sqlite3"
            initialized = runner.invoke(cli, ["--db", str(database), "init-db"])
            latest = runner.invoke(
                cli, ["--db", str(database), "latest", "--json"]
            )
            final = runner.invoke(cli, ["--db", str(database), "final"])

        self.assertEqual(0, initialized.exit_code, initialized.output)
        self.assertEqual([], json.loads(latest.output))
        self.assertEqual(1, final.exit_code)

    def test_loop_list_exposes_manifest_contract(self) -> None:
        result = CliRunner().invoke(cli, ["loop", "list", "--json"])
        self.assertEqual(0, result.exit_code, result.output)
        package = json.loads(result.output)[0]
        self.assertEqual("three-agent-dev", package["id"])
        self.assertEqual(["commander", "worker", "judger"], package["roles"])
        self.assertEqual("even-horizontal", package["layout"])

    @patch("codex_crew.cli.launch_crew")
    def test_launch_defaults_to_unix_app_server_and_exposes_thread_mapping(
        self, launch_mock
    ) -> None:
        launch_mock.return_value = _launch_result()
        result = CliRunner().invoke(cli, ["launch", "/project", "--json"])
        self.assertEqual(0, result.exit_code, result.output)
        payload = json.loads(result.output)
        self.assertEqual("thread-worker", payload["thread_mapping"]["worker"])
        self.assertNotIn("transport", payload)
        self.assertNotIn("database_path", payload)
        launch_mock.assert_called_once_with(
            Path("/project"),
            loop_id="three-agent-dev",
            app_server_endpoint="unix://",
            session="default",
            window_name=None,
        )

    @patch("codex_crew.cli.launch_crew")
    def test_launch_discovery_failure_is_nonzero_and_reports_window_role(
        self, launch_mock
    ) -> None:
        launch_mock.side_effect = LaunchError(
            "crew window @7 bootstrap discovery failed: missing roles: worker; "
            "window preserved for diagnosis"
        )
        result = CliRunner().invoke(cli, ["launch", "/project", "--json"])
        self.assertNotEqual(0, result.exit_code)
        self.assertIn("crew window @7", result.output)
        self.assertIn("missing roles: worker", result.output)

    @patch("codex_crew.cli.up_crew")
    def test_up_defaults_project_to_current_directory_and_emits_launch_json(
        self, up_mock
    ) -> None:
        up_mock.return_value = _launch_result(
            "unix:///repo/.codex-crew/runtime/app-server.sock"
        )
        result = CliRunner().invoke(cli, ["up", "--json"])

        self.assertEqual(0, result.exit_code, result.output)
        payload = json.loads(result.output)
        self.assertEqual(
            "unix:///repo/.codex-crew/runtime/app-server.sock",
            payload["app_server_endpoint"],
        )
        self.assertEqual("thread-worker", payload["thread_mapping"]["worker"])
        up_mock.assert_called_once_with(
            Path("."),
            loop_id="three-agent-dev",
            session="default",
            window_name=None,
        )

    @patch("codex_crew.cli.up_crew")
    def test_up_failure_is_nonzero_and_does_not_emit_partial_json(
        self, up_mock
    ) -> None:
        up_mock.side_effect = StartupError(
            "app-server PID 42 is alive but endpoint is not ready"
        )
        result = CliRunner().invoke(cli, ["up", "/project", "--json"])

        self.assertNotEqual(0, result.exit_code)
        self.assertIn("PID 42", result.output)
        self.assertNotIn("thread_mapping", result.output)

    @patch("codex_crew.cli.check_app_server")
    def test_app_server_check_reports_ready_endpoint(self, check_mock) -> None:
        check_mock.return_value = AppServerEndpoint(
            endpoint="unix://", socket_path=Path("/tmp/control.sock")
        )
        result = CliRunner().invoke(cli, ["app-server", "check"])
        self.assertEqual(0, result.exit_code, result.output)
        self.assertEqual(
            "ready\tendpoint=unix://\tsocket=/tmp/control.sock\n", result.output
        )

    @patch("codex_crew.cli.crew_send")
    def test_crew_send_uses_only_endpoint_and_thread_id(self, send_mock) -> None:
        send_mock.return_value = CrewCommandResult(
            command="send",
            endpoint="unix:///tmp/native.sock",
            thread_id="thread-worker",
            turn_id="turn-task",
            status="running",
            data={"dispatched": True},
        )
        result = CliRunner().invoke(
            cli,
            [
                "crew",
                "send",
                "--endpoint",
                "unix:///tmp/native.sock",
                "--thread-id",
                "thread-worker",
                "--message",
                "line one\nline two\n",
                "--json",
            ],
        )
        self.assertEqual(0, result.exit_code, result.output)
        payload = json.loads(result.output)
        self.assertEqual(2, payload["schema_version"])
        self.assertEqual("thread-worker", payload["thread_id"])
        send_mock.assert_called_once_with(
            "unix:///tmp/native.sock",
            thread_id="thread-worker",
            message="line one\nline two\n",
        )

    def test_crew_cli_rejects_removed_window_role_resolution(self) -> None:
        result = CliRunner().invoke(
            cli,
            [
                "crew",
                "status",
                "--window",
                "@7",
                "--role",
                "worker",
            ],
        )
        self.assertNotEqual(0, result.exit_code)
        self.assertIn("No such option", result.output)
        self.assertIn("--window", result.output)


if __name__ == "__main__":
    unittest.main()
