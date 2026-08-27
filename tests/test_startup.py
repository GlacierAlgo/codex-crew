from __future__ import annotations

from pathlib import Path
import socket
import subprocess
import tempfile
import unittest

from codex_crew.app_server import AppServerEndpoint, AppServerError
from codex_crew.launcher import CrewLaunch, CrewPane
from codex_crew.loop_package import load_loop_package
from codex_crew.startup import (
    StartupError,
    ensure_repo_app_server,
    repo_runtime_paths,
    up_crew,
)


ROOT = Path(__file__).resolve().parents[1]


def _completed(
    command,
    *,
    returncode: int = 0,
    stdout: str = "",
    stderr: str = "",
) -> subprocess.CompletedProcess[str]:
    return subprocess.CompletedProcess(command, returncode, stdout, stderr)


class TmuxRunner:
    def __init__(self, *, session_exists: bool = False, create_fails: bool = False):
        self.session_exists = session_exists
        self.create_fails = create_fails
        self.calls: list[tuple[str, ...]] = []

    def __call__(self, command) -> subprocess.CompletedProcess[str]:
        command = tuple(command)
        self.calls.append(command)
        operation = command[1]
        if operation == "has-session":
            return _completed(command, returncode=0 if self.session_exists else 1)
        if operation == "new-session":
            if self.create_fails:
                return _completed(command, returncode=1, stderr="tmux create failed")
            self.session_exists = True
            return _completed(command)
        raise AssertionError(f"unexpected tmux command: {command}")


class Readiness:
    def __init__(self, *, ready: bool = False):
        self.ready = ready
        self.calls: list[tuple[str, float]] = []

    def __call__(
        self, endpoint: str, *, timeout_seconds: float
    ) -> AppServerEndpoint:
        self.calls.append((endpoint, timeout_seconds))
        if not self.ready:
            raise AppServerError("not ready")
        return AppServerEndpoint(
            endpoint=endpoint,
            socket_path=Path(endpoint.removeprefix("unix://")),
        )


class FakeProcess:
    def __init__(self, *, pid: int = 4242, returncode: int | None = None):
        self.pid = pid
        self.returncode = returncode

    def poll(self) -> int | None:
        return self.returncode


class Clock:
    def __init__(self) -> None:
        self.now = 0.0

    def monotonic(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.now += seconds


class PopenRecorder:
    def __init__(
        self,
        readiness: Readiness,
        *,
        process: FakeProcess | None = None,
        mark_ready: bool = True,
    ) -> None:
        self.readiness = readiness
        self.process = process or FakeProcess()
        self.mark_ready = mark_ready
        self.calls: list[tuple[tuple[str, ...], dict]] = []

    def __call__(self, command, **kwargs) -> FakeProcess:
        self.calls.append((tuple(command), dict(kwargs)))
        if self.mark_ready:
            self.readiness.ready = True
        return self.process


class LaunchRecorder:
    def __init__(self) -> None:
        self.calls: list[tuple[Path, dict]] = []

    def __call__(self, project: Path, **kwargs) -> CrewLaunch:
        self.calls.append((project, dict(kwargs)))
        roles = load_loop_package(loops_dir=kwargs["loops_dir"]).roles
        endpoint = kwargs["app_server_endpoint"]
        return CrewLaunch(
            loop_id=kwargs["loop_id"],
            layout="even-horizontal",
            app_server_endpoint=endpoint,
            session=kwargs["session"],
            window_id="@7",
            window_index="1",
            window_name=kwargs["window_name"] or "crew-project",
            project_dir=str(project),
            panes=tuple(
                CrewPane(
                    role=role.id,
                    pane_id=f"%{index}",
                    runtime_profile=role.runtime_profile,
                    model=role.model,
                    reasoning_effort=role.reasoning_effort,
                    bootstrap_marker=f"marker-{role.id}",
                    thread_id=f"thread-{role.id}",
                    bootstrap_turn_id=f"turn-{role.id}",
                )
                for index, role in enumerate(roles, start=1)
            ),
        )


class StartupTests(unittest.TestCase):
    def test_default_runtime_paths_are_fixed_inside_the_ignored_repository_area(
        self,
    ) -> None:
        paths = repo_runtime_paths(ROOT)

        self.assertEqual(ROOT / ".codex-crew" / "runtime", paths.runtime_dir)
        self.assertEqual(paths.runtime_dir / "app-server.sock", paths.socket_path)
        self.assertEqual(paths.runtime_dir / "app-server.pid", paths.pid_path)
        self.assertEqual(paths.runtime_dir / "app-server.log", paths.log_path)
        self.assertEqual(f"unix://{paths.socket_path}", paths.endpoint)
        self.assertIn(".codex-crew/", (ROOT / ".gitignore").read_text(encoding="utf-8"))

    def test_up_is_idempotent_across_profiles_server_and_exact_tmux_session(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            repository = root / "repo"
            project = root / "project"
            codex_home = root / "codex-home"
            repository.mkdir()
            project.mkdir()
            (project / "README.md").write_text("# Product\n", encoding="utf-8")
            tmux = TmuxRunner()
            readiness = Readiness()
            popen = PopenRecorder(readiness)
            launcher = LaunchRecorder()

            first = up_crew(
                project,
                repo_root=repository,
                loops_dir=ROOT / "loops",
                codex_home=codex_home,
                tmux_executable="/fake/tmux",
                codex_executable="/fake/codex",
                command_runner=tmux,
                popen_factory=popen,
                readiness_check=readiness,
                launcher=launcher,
            )
            second = up_crew(
                project,
                repo_root=repository,
                loops_dir=ROOT / "loops",
                codex_home=codex_home,
                tmux_executable="/fake/tmux",
                codex_executable="/fake/codex",
                command_runner=tmux,
                popen_factory=popen,
                readiness_check=readiness,
                launcher=launcher,
            )

            paths = repo_runtime_paths(repository)
            package = load_loop_package(loops_dir=ROOT / "loops")
            new_sessions = [call for call in tmux.calls if call[1] == "new-session"]
            self.assertEqual(1, len(new_sessions))
            self.assertEqual(
                (
                    "/fake/tmux",
                    "new-session",
                    "-d",
                    "-s",
                    "default",
                    "-c",
                    str(project.resolve()),
                ),
                new_sessions[0],
            )
            self.assertEqual(1, len(popen.calls))
            command, options = popen.calls[0]
            self.assertEqual(
                (
                    "/fake/codex",
                    "app-server",
                    "--listen",
                    paths.endpoint,
                ),
                command,
            )
            self.assertIs(subprocess.DEVNULL, options["stdin"])
            self.assertEqual(paths.log_path, Path(options["stdout"].name))
            self.assertIs(subprocess.STDOUT, options["stderr"])
            self.assertTrue(options["start_new_session"])
            self.assertEqual("4242\n", paths.pid_path.read_text(encoding="ascii"))
            self.assertTrue(paths.log_path.is_file())
            self.assertEqual(paths.endpoint, first.app_server_endpoint)
            self.assertEqual(paths.endpoint, second.app_server_endpoint)
            self.assertEqual(2, len(launcher.calls))
            for _, launch_options in launcher.calls:
                self.assertEqual(paths.endpoint, launch_options["app_server_endpoint"])
                self.assertEqual("/fake/codex", launch_options["codex_executable"])
                self.assertEqual("/fake/tmux", launch_options["tmux_executable"])
            for role in package.roles:
                symlink = codex_home / f"{role.runtime_profile}.config.toml"
                self.assertTrue(symlink.is_symlink())
                self.assertEqual(
                    repository.resolve() / ".codex-crew" / "generated",
                    symlink.resolve().parents[1],
                )

    def test_profile_conflict_fails_before_tmux_server_and_launch(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            repository = root / "repo"
            project = root / "project"
            codex_home = root / "codex-home"
            repository.mkdir()
            project.mkdir()
            codex_home.mkdir()
            (project / "README.md").write_text("# Product\n", encoding="utf-8")
            profile = load_loop_package(loops_dir=ROOT / "loops").roles[0]
            conflict = codex_home / f"{profile.runtime_profile}.config.toml"
            conflict.write_text("user owned\n", encoding="utf-8")
            tmux = TmuxRunner()
            readiness = Readiness()
            popen = PopenRecorder(readiness)
            launcher = LaunchRecorder()

            with self.assertRaisesRegex(StartupError, "profile setup failed"):
                up_crew(
                    project,
                    repo_root=repository,
                    loops_dir=ROOT / "loops",
                    codex_home=codex_home,
                    tmux_executable="/fake/tmux",
                    codex_executable="/fake/codex",
                    command_runner=tmux,
                    popen_factory=popen,
                    readiness_check=readiness,
                    launcher=launcher,
                )

            self.assertEqual([], tmux.calls)
            self.assertEqual([], readiness.calls)
            self.assertEqual([], popen.calls)
            self.assertEqual([], launcher.calls)
            self.assertEqual("user owned\n", conflict.read_text(encoding="utf-8"))

    def test_tmux_create_failure_stops_before_server_and_launch(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            repository = root / "repo"
            project = root / "project"
            repository.mkdir()
            project.mkdir()
            (project / "README.md").write_text("# Product\n", encoding="utf-8")
            tmux = TmuxRunner(create_fails=True)
            readiness = Readiness()
            popen = PopenRecorder(readiness)
            launcher = LaunchRecorder()

            with self.assertRaisesRegex(StartupError, "tmux create failed"):
                up_crew(
                    project,
                    repo_root=repository,
                    loops_dir=ROOT / "loops",
                    codex_home=root / "codex-home",
                    tmux_executable="/fake/tmux",
                    codex_executable="/fake/codex",
                    command_runner=tmux,
                    popen_factory=popen,
                    readiness_check=readiness,
                    launcher=launcher,
                )

            self.assertEqual([], readiness.calls)
            self.assertEqual([], popen.calls)
            self.assertEqual([], launcher.calls)

    def test_server_exit_before_ready_stops_before_launch_with_log_path(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            repository = root / "repo"
            project = root / "project"
            repository.mkdir()
            project.mkdir()
            (project / "README.md").write_text("# Product\n", encoding="utf-8")
            tmux = TmuxRunner()
            readiness = Readiness()
            popen = PopenRecorder(
                readiness,
                process=FakeProcess(returncode=78),
                mark_ready=False,
            )
            launcher = LaunchRecorder()

            with self.assertRaises(StartupError) as caught:
                up_crew(
                    project,
                    repo_root=repository,
                    loops_dir=ROOT / "loops",
                    codex_home=root / "codex-home",
                    tmux_executable="/fake/tmux",
                    codex_executable="/fake/codex",
                    command_runner=tmux,
                    popen_factory=popen,
                    readiness_check=readiness,
                    launcher=launcher,
                )

            message = str(caught.exception)
            self.assertIn("exited with status 78", message)
            self.assertIn("app-server.log", message)
            self.assertEqual([], launcher.calls)

    def test_ready_repo_endpoint_is_reused_without_starting_process(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            repository = Path(directory)
            paths = repo_runtime_paths(repository)
            readiness = Readiness(ready=True)
            popen = PopenRecorder(readiness)

            result = ensure_repo_app_server(
                paths,
                codex_executable="/fake/codex",
                popen_factory=popen,
                readiness_check=readiness,
            )

            self.assertFalse(result.started)
            self.assertIsNone(result.pid)
            self.assertEqual([], popen.calls)

    def test_live_pid_with_unready_endpoint_fails_closed_without_kill_or_start(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            repository = Path(directory)
            paths = repo_runtime_paths(repository)
            paths.runtime_dir.mkdir(parents=True)
            paths.pid_path.write_text("777\n", encoding="ascii")
            readiness = Readiness()
            popen = PopenRecorder(readiness)

            with self.assertRaises(StartupError) as caught:
                ensure_repo_app_server(
                    paths,
                    codex_executable="/fake/codex",
                    popen_factory=popen,
                    readiness_check=readiness,
                    process_alive=lambda pid: pid == 777,
                )

            self.assertIn("refusing to kill", str(caught.exception))
            self.assertEqual("777\n", paths.pid_path.read_text(encoding="ascii"))
            self.assertEqual([], popen.calls)

    def test_started_live_process_times_out_bounded_and_is_left_for_diagnosis(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            repository = Path(directory)
            paths = repo_runtime_paths(repository)
            readiness = Readiness()
            popen = PopenRecorder(readiness, mark_ready=False)
            clock = Clock()

            with self.assertRaises(StartupError) as caught:
                ensure_repo_app_server(
                    paths,
                    codex_executable="/fake/codex",
                    popen_factory=popen,
                    readiness_check=readiness,
                    timeout_seconds=0.2,
                    poll_seconds=0.1,
                    sleep=clock.sleep,
                    monotonic=clock.monotonic,
                )

            message = str(caught.exception)
            self.assertIn("was not ready within 0.2s", message)
            self.assertIn("refusing to kill it", message)
            self.assertEqual("4242\n", paths.pid_path.read_text(encoding="ascii"))
            self.assertEqual(1, len(popen.calls))

    def test_dead_owned_pid_and_socket_are_cleaned_before_one_restart(self) -> None:
        with tempfile.TemporaryDirectory(dir="/tmp", prefix="cc-") as directory:
            repository = Path(directory)
            paths = repo_runtime_paths(repository)
            paths.runtime_dir.mkdir(parents=True)
            paths.pid_path.write_text("777\n", encoding="ascii")
            with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as stale:
                stale.bind(str(paths.socket_path))
            readiness = Readiness()
            popen = PopenRecorder(readiness)

            result = ensure_repo_app_server(
                paths,
                codex_executable="/fake/codex",
                popen_factory=popen,
                readiness_check=readiness,
                process_alive=lambda pid: False,
            )

            self.assertTrue(result.started)
            self.assertEqual(4242, result.pid)
            self.assertFalse(paths.socket_path.exists())
            self.assertEqual("4242\n", paths.pid_path.read_text(encoding="ascii"))
            self.assertEqual(1, len(popen.calls))

    def test_orphan_socket_is_not_removed_or_replaced(self) -> None:
        with tempfile.TemporaryDirectory(dir="/tmp", prefix="cc-") as directory:
            repository = Path(directory)
            paths = repo_runtime_paths(repository)
            paths.runtime_dir.mkdir(parents=True)
            with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as stale:
                stale.bind(str(paths.socket_path))
            readiness = Readiness()
            popen = PopenRecorder(readiness)

            with self.assertRaisesRegex(StartupError, "without an owned PID file"):
                ensure_repo_app_server(
                    paths,
                    codex_executable="/fake/codex",
                    popen_factory=popen,
                    readiness_check=readiness,
                )

            self.assertTrue(paths.socket_path.exists())
            self.assertEqual([], popen.calls)


if __name__ == "__main__":
    unittest.main()
