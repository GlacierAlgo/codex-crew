from __future__ import annotations

import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from click.testing import CliRunner

from codex_crew.cli import cli
from codex_crew.launcher import CrewLaunch, CrewPane


def _launch_result() -> CrewLaunch:
    return CrewLaunch(
        loop_id="three-agent-dev",
        layout="even-horizontal",
        session="default",
        window_id="@7",
        window_index="6",
        window_name="crew-project",
        project_dir="/project",
        panes=(
            CrewPane(
                role="commander",
                pane_id="%10",
                runtime_profile="codex-crew-three-agent-dev-commander",
                model="gpt-5.6-sol",
                reasoning_effort="xhigh",
            ),
            CrewPane(
                role="worker",
                pane_id="%11",
                runtime_profile="codex-crew-three-agent-dev-worker",
                model="gpt-5.6-sol",
                reasoning_effort="xhigh",
            ),
            CrewPane(
                role="judger",
                pane_id="%12",
                runtime_profile="codex-crew-three-agent-dev-judger",
                model="gpt-5.6-sol",
                reasoning_effort="xhigh",
            ),
        ),
    )


class ClickCliTests(unittest.TestCase):
    def test_init_db_and_latest_keep_existing_command_contract(self) -> None:
        runner = CliRunner()
        with tempfile.TemporaryDirectory() as directory:
            database = Path(directory) / "snapshots.sqlite3"
            initialized = runner.invoke(cli, ["--db", str(database), "init-db"])
            latest = runner.invoke(
                cli,
                ["--db", str(database), "latest", "--json"],
            )

        self.assertEqual(0, initialized.exit_code, initialized.output)
        self.assertEqual(0, latest.exit_code, latest.output)
        self.assertEqual([], json.loads(latest.output))

    def test_final_keeps_exit_one_when_no_message_exists(self) -> None:
        runner = CliRunner()
        with tempfile.TemporaryDirectory() as directory:
            database = Path(directory) / "snapshots.sqlite3"
            result = runner.invoke(cli, ["--db", str(database), "final"])

        self.assertEqual(1, result.exit_code)
        self.assertEqual("", result.output)

    def test_loop_list_exposes_manifest_contract(self) -> None:
        result = CliRunner().invoke(cli, ["loop", "list", "--json"])

        self.assertEqual(0, result.exit_code, result.output)
        packages = json.loads(result.output)
        self.assertEqual("three-agent-dev", packages[0]["id"])
        self.assertEqual(
            ["commander", "worker", "judger"], packages[0]["roles"]
        )
        self.assertEqual("even-horizontal", packages[0]["layout"])

    @patch("codex_crew.loop_package.default_managed_root")
    def test_loop_install_and_check_use_codex_home_environment(
        self, managed_root_mock
    ) -> None:
        runner = CliRunner()
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            managed_root_mock.return_value = root / "managed"
            codex_home = root / "codex-home"
            environment = {"CODEX_HOME": str(codex_home)}

            installed = runner.invoke(
                cli, ["loop", "install"], env=environment
            )
            checked = runner.invoke(cli, ["loop", "check"], env=environment)
            installed_again = runner.invoke(
                cli, ["loop", "install"], env=environment
            )

        self.assertEqual(0, installed.exit_code, installed.output)
        self.assertEqual(0, checked.exit_code, checked.output)
        self.assertEqual(0, installed_again.exit_code, installed_again.output)
        self.assertEqual(installed.output, installed_again.output)
        self.assertIn("3 managed profiles", checked.output)

    @patch("codex_crew.cli.launch_crew")
    def test_launch_json_exposes_generic_stable_pane_mapping(
        self, launch_mock
    ) -> None:
        launch_mock.return_value = _launch_result()

        result = CliRunner().invoke(cli, ["launch", "/project", "--json"])

        self.assertEqual(0, result.exit_code, result.output)
        payload = json.loads(result.output)
        self.assertEqual("%11", payload["pane_mapping"]["worker"])
        self.assertEqual(
            "codex-crew-three-agent-dev-worker",
            payload["panes"][1]["runtime_profile"],
        )
        launch_mock.assert_called_once_with(
            Path("/project"),
            loop_id="three-agent-dev",
            session="default",
            window_name=None,
        )

    @patch("codex_crew.cli.launch_crew")
    def test_launch_passes_explicit_loop_selection(self, launch_mock) -> None:
        launch_mock.return_value = _launch_result()

        result = CliRunner().invoke(
            cli, ["launch", "/project", "--loop", "custom-loop"]
        )

        self.assertEqual(0, result.exit_code, result.output)
        launch_mock.assert_called_once_with(
            Path("/project"),
            loop_id="custom-loop",
            session="default",
            window_name=None,
        )


if __name__ == "__main__":
    unittest.main()
