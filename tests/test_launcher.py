from __future__ import annotations

from pathlib import Path
import shlex
import subprocess
import tempfile
import unittest

from codex_crew.launcher import LaunchError, launch_crew


def _completed(
    command,
    *,
    returncode: int = 0,
    stdout: str = "",
    stderr: str = "",
) -> subprocess.CompletedProcess[str]:
    return subprocess.CompletedProcess(command, returncode, stdout, stderr)


class FakeRunner:
    def __init__(self, *, fail_first_split: bool = False) -> None:
        self.calls: list[tuple[str, ...]] = []
        self.fail_first_split = fail_first_split
        self.horizontal_splits = 0

    def __call__(self, command) -> subprocess.CompletedProcess[str]:
        call = tuple(command)
        self.calls.append(call)
        if call[0] == "/codex":
            return _completed(call, stdout="codex-cli test\n")
        operation = call[1]
        if operation == "has-session":
            return _completed(call)
        if operation == "new-window":
            return _completed(call, stdout="@7\t%10\t6\n")
        if operation == "split-window" and "-h" in call:
            self.horizontal_splits += 1
            if self.fail_first_split and self.horizontal_splits == 1:
                return _completed(call, returncode=1, stderr="no space")
            pane_id = f"%{10 + self.horizontal_splits}"
            return _completed(call, stdout=f"{pane_id}\n")
        if operation in {"select-layout", "set-option", "kill-window"}:
            return _completed(call)
        raise AssertionError(f"unexpected command: {call}")


class LauncherTests(unittest.TestCase):
    def test_launch_uses_manifest_policy_and_records_stable_mapping(self) -> None:
        runner = FakeRunner()
        with tempfile.TemporaryDirectory() as directory:
            project = Path(directory)
            (project / "README.md").write_text("# Product\n", encoding="utf-8")

            result = launch_crew(
                project,
                tmux_executable="/tmux",
                codex_executable="/codex",
                runner=runner,
            )

        self.assertEqual("three-agent-dev", result.loop_id)
        self.assertEqual("even-horizontal", result.layout)
        self.assertEqual(
            {"commander": "%10", "worker": "%11", "judger": "%12"},
            result.pane_mapping,
        )
        self.assertEqual(
            [
                "codex-crew-three-agent-dev-commander",
                "codex-crew-three-agent-dev-worker",
                "codex-crew-three-agent-dev-judger",
            ],
            [pane.runtime_profile for pane in result.panes],
        )
        self.assertEqual({"gpt-5.6-sol"}, {pane.model for pane in result.panes})
        self.assertEqual({"xhigh"}, {pane.reasoning_effort for pane in result.panes})

        new_window = next(call for call in runner.calls if call[1] == "new-window")
        horizontal_splits = [
            call
            for call in runner.calls
            if call[1] == "split-window" and "-h" in call
        ]
        self.assertEqual(2, len(horizontal_splits))
        worker_split, judger_split = horizontal_splits
        self.assertEqual("=default:", new_window[new_window.index("-t") + 1])
        self.assertEqual("%10", worker_split[worker_split.index("-t") + 1])
        self.assertEqual("%11", judger_split[judger_split.index("-t") + 1])
        self.assertNotIn("-v", judger_split)
        self.assertIn(
            ("/tmux", "select-layout", "-t", "@7", "even-horizontal"),
            runner.calls,
        )
        self.assertEqual(
            shlex.join(
                [
                    "/codex",
                    "--profile",
                    "codex-crew-three-agent-dev-commander",
                    "--strict-config",
                    "-C",
                    result.project_dir,
                ]
            ),
            new_window[-1],
        )
        self.assertIn(
            (
                "/tmux",
                "set-option",
                "-w",
                "-t",
                "@7",
                "@codex_worker_pane",
                "%11",
            ),
            runner.calls,
        )
        self.assertIn(
            (
                "/tmux",
                "set-option",
                "-p",
                "-t",
                "%12",
                "@codex_role",
                "judger",
            ),
            runner.calls,
        )
        profile_checks = [call for call in runner.calls if call[0] == "/codex"]
        self.assertEqual(3, len(profile_checks))
        self.assertEqual(
            "codex-crew-three-agent-dev-worker", profile_checks[1][2]
        )

    def test_failure_after_window_creation_rolls_back_exact_window(self) -> None:
        runner = FakeRunner(fail_first_split=True)
        with tempfile.TemporaryDirectory() as directory:
            project = Path(directory)
            (project / "README.md").write_text("# Product\n", encoding="utf-8")

            with self.assertRaisesRegex(LaunchError, "worker pane"):
                launch_crew(
                    project,
                    tmux_executable="/tmux",
                    codex_executable="/codex",
                    runner=runner,
                )

        self.assertEqual(
            ("/tmux", "kill-window", "-t", "@7"),
            runner.calls[-1],
        )

    def test_explicit_unknown_loop_fails_before_external_commands(self) -> None:
        runner = FakeRunner()
        with tempfile.TemporaryDirectory() as directory:
            project = Path(directory)
            (project / "README.md").write_text("# Product\n", encoding="utf-8")

            with self.assertRaisesRegex(LaunchError, "unknown loop package"):
                launch_crew(
                    project,
                    loop_id="missing-loop",
                    tmux_executable="/tmux",
                    codex_executable="/codex",
                    runner=runner,
                )

        self.assertEqual([], runner.calls)

    def test_target_requires_root_readme_for_black_box_judger(self) -> None:
        runner = FakeRunner()
        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaisesRegex(LaunchError, "no README.md"):
                launch_crew(
                    directory,
                    tmux_executable="/tmux",
                    codex_executable="/codex",
                    runner=runner,
                )

        self.assertEqual([], runner.calls)


if __name__ == "__main__":
    unittest.main()
