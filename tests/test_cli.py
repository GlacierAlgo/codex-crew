from __future__ import annotations

import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from click.testing import CliRunner

from codex_crew.app_server import AppServerEndpoint
from codex_crew.cli import cli, crew_entrypoint
from codex_crew.crew_runtime import CrewCommandResult
from codex_crew.launcher import CrewLaunch, CrewPane, LaunchError
from codex_crew.lifecycle import CloseResult
from codex_crew.startup import StartupError


def _launch_result(
    endpoint: str = "unix://",
    *,
    loop_id: str = "three-agent-dev",
    roles: tuple[str, ...] = ("commander", "worker", "judger"),
    communication_role: str = "commander",
    layout: str = "even-horizontal",
) -> CrewLaunch:
    return CrewLaunch(
        loop_id=loop_id,
        layout=layout,
        app_server_endpoint=endpoint,
        session="default",
        window_id="@7",
        window_index="6",
        window_name="crew-project",
        project_dir="/project",
        communication_role=communication_role,
        handoff_turn_id=f"turn-handoff-{communication_role}",
        handoff_status="completed",
        lifecycle_record_path="/repo/.codex-crew/runtime/crew-lifecycle/window-7.json",
        close_command="codex-crew crew close --window-id @7",
        panes=tuple(
            CrewPane(
                role=role,
                pane_id=f"%{10 + index}",
                runtime_profile=f"codex-crew-{loop_id}-{role.replace('_', '-')}",
                model="gpt-5.6-sol",
                reasoning_effort="high",
                service_tier="fast",
                bootstrap_marker=f"marker-{role}",
                thread_id=f"thread-{role}",
                bootstrap_turn_id=f"turn-{role}",
            )
            for index, role in enumerate(roles)
        ),
    )


class ClickCliTests(unittest.TestCase):
    @patch("codex_crew.cli.up_crew")
    def test_crew_entrypoint_defaults_to_resolved_invocation_cwd(
        self, up_mock
    ) -> None:
        up_mock.return_value = _launch_result(
            "unix:///repo/.codex-crew/runtime/app-server.sock"
        )
        runner = CliRunner()
        with runner.isolated_filesystem() as directory:
            invocation_cwd = Path(directory).resolve()
            result = runner.invoke(
                crew_entrypoint,
                ["three-agent-dev", "--json"],
            )

        self.assertEqual(0, result.exit_code, result.output)
        up_mock.assert_called_once_with(
            invocation_cwd,
            loop_id="three-agent-dev",
            session="default",
            window_name=None,
        )

    @patch("codex_crew.cli.up_crew")
    def test_crew_entrypoint_explicit_project_overrides_cwd(self, up_mock) -> None:
        up_mock.return_value = _launch_result(
            "unix:///repo/.codex-crew/runtime/app-server.sock",
            loop_id="api-budget-design",
            roles=(
                "commander",
                "worker_3",
                "worker_4",
                "worker_5",
                "worker_6",
            ),
            communication_role="commander",
            layout="split-plan",
        )
        with tempfile.TemporaryDirectory() as directory:
            project = Path(directory) / "target"
            project.mkdir()
            result = CliRunner().invoke(
                crew_entrypoint,
                [
                    "api-budget-design",
                    str(project),
                    "--session",
                    "research",
                    "--window-name",
                    "explicit-name",
                    "--json",
                ],
            )

        self.assertEqual(0, result.exit_code, result.output)
        up_mock.assert_called_once_with(
            project.resolve(),
            loop_id="api-budget-design",
            session="research",
            window_name="explicit-name",
        )

    def test_crew_loop_completion_is_registry_backed(self) -> None:
        result = CliRunner().invoke(
            crew_entrypoint,
            [],
            env={
                "_CREW_COMPLETE": "bash_complete",
                "COMP_WORDS": "crew ",
                "COMP_CWORD": "1",
            },
        )

        self.assertEqual(0, result.exit_code, result.output)
        self.assertEqual(
            {"plain,api-budget-design", "plain,three-agent-dev"},
            set(result.output.splitlines()),
        )

    def test_crew_show_completion_emits_click_source(self) -> None:
        markers = {
            "zsh": "#compdef crew",
            "bash": "complete -o nosort",
            "fish": "complete --no-files --command crew",
        }
        for shell, marker in markers.items():
            with self.subTest(shell=shell):
                result = CliRunner().invoke(
                    crew_entrypoint,
                    ["--show-completion", shell],
                )
                self.assertEqual(0, result.exit_code, result.output)
                self.assertIn(marker, result.output)
                self.assertIn("_CREW_COMPLETE", result.output)

    def test_crew_invalid_loop_is_clear_nonzero_error(self) -> None:
        result = CliRunner().invoke(crew_entrypoint, ["missing-loop"])

        self.assertNotEqual(0, result.exit_code)
        self.assertIn("unknown loop package 'missing-loop'", result.output)
        self.assertIn("api-budget-design", result.output)
        self.assertIn("three-agent-dev", result.output)

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
        packages = {
            package["id"]: package for package in json.loads(result.output)
        }
        self.assertEqual(
            {"api-budget-design", "three-agent-dev"}, set(packages)
        )
        self.assertEqual(
            ["commander", "worker", "judger"],
            packages["three-agent-dev"]["roles"],
        )
        self.assertEqual("commander", packages["three-agent-dev"]["communication_role"])
        self.assertEqual(
            [
                "commander",
                "worker_3",
                "worker_4",
                "worker_5",
                "worker_6",
            ],
            packages["api-budget-design"]["roles"],
        )
        self.assertEqual(
            "commander", packages["api-budget-design"]["communication_role"]
        )
        self.assertEqual("even-horizontal", packages["three-agent-dev"]["layout"])
        self.assertEqual("split-plan", packages["api-budget-design"]["layout"])

    @patch("codex_crew.cli.launch_crew")
    def test_launch_defaults_to_unix_app_server_and_exposes_thread_mapping(
        self, launch_mock
    ) -> None:
        launch_mock.return_value = _launch_result()
        result = CliRunner().invoke(cli, ["launch", "/project", "--json"])
        self.assertEqual(0, result.exit_code, result.output)
        payload = json.loads(result.output)
        self.assertEqual("thread-worker", payload["thread_mapping"]["worker"])
        self.assertEqual("commander", payload["communication_role"])
        self.assertEqual("thread-commander", payload["communication_thread_id"])
        self.assertEqual("%10", payload["communication_pane_id"])
        self.assertEqual("turn-handoff-commander", payload["handoff_turn_id"])
        self.assertEqual("completed", payload["handoff_status"])
        self.assertEqual(
            "codex-crew crew close --window-id @7", payload["close_command"]
        )
        self.assertTrue(payload["lifecycle_record_path"].endswith("window-7.json"))
        self.assertEqual(
            {"fast"}, {pane["service_tier"] for pane in payload["panes"]}
        )
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
    def test_up_accepts_explicit_five_role_api_budget_loop(self, up_mock) -> None:
        roles = (
            "commander",
            "worker_3",
            "worker_4",
            "worker_5",
            "worker_6",
        )
        up_mock.return_value = _launch_result(
            "unix:///repo/.codex-crew/runtime/app-server.sock",
            loop_id="api-budget-design",
            roles=roles,
            communication_role="commander",
            layout="split-plan",
        )
        result = CliRunner().invoke(
            cli,
            [
                "up",
                "/project",
                "--loop",
                "api-budget-design",
                "--json",
            ],
        )

        self.assertEqual(0, result.exit_code, result.output)
        payload = json.loads(result.output)
        self.assertEqual("api-budget-design", payload["loop_id"])
        self.assertEqual(
            {role: f"thread-{role}" for role in roles},
            payload["thread_mapping"],
        )
        self.assertEqual("commander", payload["communication_role"])
        self.assertEqual("thread-commander", payload["communication_thread_id"])
        self.assertEqual(
            {"fast"}, {pane["service_tier"] for pane in payload["panes"]}
        )
        up_mock.assert_called_once_with(
            Path("/project"),
            loop_id="api-budget-design",
            session="default",
            window_name=None,
        )

    @patch("codex_crew.cli.launch_crew")
    def test_launch_human_output_exposes_service_tier(self, launch_mock) -> None:
        launch_mock.return_value = _launch_result()

        result = CliRunner().invoke(cli, ["launch", "/project"])

        self.assertEqual(0, result.exit_code, result.output)
        self.assertEqual(3, result.output.count("service_tier=fast"))
        self.assertEqual(3, result.output.count("reasoning_effort=high"))
        self.assertIn(
            "communication: role=commander pane=%10 thread=thread-commander "
            "handoff_turn=turn-handoff-commander handoff_status=completed",
            result.output,
        )
        self.assertIn("lifecycle-record:", result.output)
        self.assertIn(
            "close: codex-crew crew close --window-id @7", result.output
        )

    def test_crew_close_help_requires_exact_window_and_has_no_delete_option(self) -> None:
        result = CliRunner().invoke(cli, ["crew", "close", "--help"])

        self.assertEqual(0, result.exit_code, result.output)
        self.assertIn("--window-id", result.output)
        self.assertNotIn("--delete", result.output)

    @patch("codex_crew.cli.close_crew")
    def test_crew_close_json_uses_only_exact_window_record(self, close_mock) -> None:
        close_mock.return_value = CloseResult(
            window_id="@7",
            record_path="/repo/.codex-crew/runtime/crew-lifecycle/window-7.json",
            archived=(("worker", "thread-worker"), ("commander", "thread-commander")),
        )

        result = CliRunner().invoke(
            cli, ["crew", "close", "--window-id", "@7", "--json"]
        )

        self.assertEqual(0, result.exit_code, result.output)
        payload = json.loads(result.output)
        self.assertEqual("closed", payload["status"])
        self.assertEqual("@7", payload["window_id"])
        close_mock.assert_called_once_with("@7")

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
