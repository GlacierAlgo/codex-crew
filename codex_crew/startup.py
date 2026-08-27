"""Repository-owned one-click startup for a native-thread Codex crew."""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass
import errno
import os
from pathlib import Path
import shutil
import stat
import subprocess
import tempfile
import time
from typing import Any

from codex_crew.app_server import AppServerEndpoint, AppServerError, check_app_server
from codex_crew.launcher import CrewLaunch, LaunchError, launch_crew
from codex_crew.lifecycle import LIFECYCLE_DIR_NAME
from codex_crew.loop_package import (
    DEFAULT_LOOP_ID,
    LoopPackageError,
    check_loop_installation,
    install_loop_package,
    load_loop_package,
    repository_root,
)


RUNTIME_RELATIVE_PATH = Path(".codex-crew/runtime")
APP_SERVER_SOCKET_NAME = "app-server.sock"
APP_SERVER_PID_NAME = "app-server.pid"
APP_SERVER_LOG_NAME = "app-server.log"
DEFAULT_READY_TIMEOUT_SECONDS = 10.0
DEFAULT_READY_POLL_SECONDS = 0.1
DEFAULT_READY_PROBE_TIMEOUT_SECONDS = 0.2


class StartupError(RuntimeError):
    """A one-click startup gate failed before a crew launch completed."""


@dataclass(frozen=True)
class RepoRuntimePaths:
    repository: Path
    runtime_dir: Path
    socket_path: Path
    pid_path: Path
    log_path: Path
    lifecycle_dir: Path
    endpoint: str


@dataclass(frozen=True)
class ReadyAppServer:
    paths: RepoRuntimePaths
    pid: int | None
    started: bool

    @property
    def endpoint(self) -> str:
        return self.paths.endpoint


CommandRunner = Callable[[Sequence[str]], subprocess.CompletedProcess[str]]
ReadyCheck = Callable[..., AppServerEndpoint]
ProcessAlive = Callable[[int], bool]
PopenFactory = Callable[..., Any]
CrewLauncher = Callable[..., CrewLaunch]


def repo_runtime_paths(repo_root: str | Path | None = None) -> RepoRuntimePaths:
    """Return the only paths owned by the repository App Server lifecycle."""

    root = _repository_path(repo_root)
    runtime_dir = root / RUNTIME_RELATIVE_PATH
    socket_path = runtime_dir / APP_SERVER_SOCKET_NAME
    return RepoRuntimePaths(
        repository=root,
        runtime_dir=runtime_dir,
        socket_path=socket_path,
        pid_path=runtime_dir / APP_SERVER_PID_NAME,
        log_path=runtime_dir / APP_SERVER_LOG_NAME,
        lifecycle_dir=runtime_dir / LIFECYCLE_DIR_NAME,
        endpoint=f"unix://{socket_path}",
    )


def up_crew(
    project_dir: str | Path = ".",
    *,
    loop_id: str = DEFAULT_LOOP_ID,
    session: str = "default",
    window_name: str | None = None,
    codex_home: str | Path | None = None,
    repo_root: str | Path | None = None,
    loops_dir: str | Path | None = None,
    tmux_executable: str | None = None,
    codex_executable: str | None = None,
    command_runner: CommandRunner | None = None,
    popen_factory: PopenFactory = subprocess.Popen,
    readiness_check: ReadyCheck = check_app_server,
    process_alive: ProcessAlive | None = None,
    ready_timeout_seconds: float = DEFAULT_READY_TIMEOUT_SECONDS,
    ready_poll_seconds: float = DEFAULT_READY_POLL_SECONDS,
    sleep: Callable[[float], None] = time.sleep,
    monotonic: Callable[[], float] = time.monotonic,
    launcher: CrewLauncher = launch_crew,
) -> CrewLaunch:
    """Prepare profiles, tmux, and App Server, then run the native launcher."""

    paths = repo_runtime_paths(repo_root)
    try:
        package = load_loop_package(loop_id, loops_dir=loops_dir)
    except LoopPackageError as error:
        raise StartupError(str(error)) from error
    project = _project_path(project_dir)

    managed_root = paths.repository / ".codex-crew" / "generated"
    try:
        install_loop_package(
            package,
            codex_home=codex_home,
            managed_root=managed_root,
        )
        check_loop_installation(
            package,
            codex_home=codex_home,
            managed_root=managed_root,
        )
    except LoopPackageError as error:
        raise StartupError(f"profile setup failed: {error}") from error

    tmux = _resolve_executable("tmux", tmux_executable)
    codex = _resolve_executable("codex", codex_executable)
    execute = command_runner or _run_command
    ensure_tmux_session(
        project,
        session=session,
        tmux_executable=tmux,
        runner=execute,
    )
    server = ensure_repo_app_server(
        paths,
        codex_executable=codex,
        popen_factory=popen_factory,
        readiness_check=readiness_check,
        process_alive=process_alive,
        timeout_seconds=ready_timeout_seconds,
        poll_seconds=ready_poll_seconds,
        sleep=sleep,
        monotonic=monotonic,
    )

    try:
        return launcher(
            project,
            loop_id=package.id,
            session=session,
            window_name=window_name,
            app_server_endpoint=server.endpoint,
            tmux_executable=tmux,
            codex_executable=codex,
            runner=execute,
            loops_dir=loops_dir,
            lifecycle_dir=paths.lifecycle_dir,
        )
    except LaunchError as error:
        raise StartupError(str(error)) from error


def ensure_tmux_session(
    project_dir: str | Path,
    *,
    session: str,
    tmux_executable: str,
    runner: CommandRunner,
) -> bool:
    """Create one exact tmux session only when it does not already exist."""

    project = _project_path(project_dir)
    _validate_session_name(session)
    probe = _execute(
        runner,
        [tmux_executable, "has-session", "-t", f"={session}"],
        "could not inspect the tmux session",
    )
    if probe.returncode == 0:
        return False

    created = _execute(
        runner,
        [
            tmux_executable,
            "new-session",
            "-d",
            "-s",
            session,
            "-c",
            str(project),
        ],
        f"failed to create tmux session {session!r}",
    )
    if created.returncode != 0:
        detail = (created.stderr or created.stdout or "").strip()
        suffix = f": {detail}" if detail else ""
        raise StartupError(f"failed to create tmux session {session!r}{suffix}")
    return True


def ensure_repo_app_server(
    paths: RepoRuntimePaths,
    *,
    codex_executable: str,
    popen_factory: PopenFactory = subprocess.Popen,
    readiness_check: ReadyCheck = check_app_server,
    process_alive: ProcessAlive | None = None,
    timeout_seconds: float = DEFAULT_READY_TIMEOUT_SECONDS,
    poll_seconds: float = DEFAULT_READY_POLL_SECONDS,
    sleep: Callable[[float], None] = time.sleep,
    monotonic: Callable[[], float] = time.monotonic,
) -> ReadyAppServer:
    """Reuse or start the fenced App Server in the repository runtime area."""

    if timeout_seconds <= 0:
        raise StartupError("app-server readiness timeout must be greater than zero")
    if poll_seconds <= 0:
        raise StartupError("app-server readiness poll interval must be greater than zero")
    _ensure_runtime_directory(paths)

    ready, _ = _probe_ready(
        readiness_check,
        paths.endpoint,
        timeout_seconds=min(DEFAULT_READY_PROBE_TIMEOUT_SECONDS, timeout_seconds),
    )
    if ready:
        return ReadyAppServer(paths=paths, pid=None, started=False)

    existing_pid = _optional_pid(paths.pid_path)
    alive = process_alive or _process_is_alive
    if existing_pid is not None:
        if alive(existing_pid):
            raise StartupError(
                f"app-server PID {existing_pid} is alive but endpoint "
                f"{paths.endpoint} is not ready; refusing to kill or launch over it; "
                f"inspect {paths.log_path}"
            )
        _clean_dead_runtime(paths)
    elif _lexists(paths.socket_path):
        raise StartupError(
            f"app-server socket exists without an owned PID file: {paths.socket_path}; "
            "refusing to remove or replace it"
        )

    _validate_log_target(paths.log_path)
    command = [codex_executable, "app-server", "--listen", paths.endpoint]
    try:
        with paths.log_path.open("ab") as log_stream:
            process = popen_factory(
                command,
                stdin=subprocess.DEVNULL,
                stdout=log_stream,
                stderr=subprocess.STDOUT,
                start_new_session=True,
            )
    except (OSError, subprocess.SubprocessError) as error:
        raise StartupError(
            f"failed to start repo app-server at {paths.endpoint}: {error}; "
            f"log={paths.log_path}"
        ) from error

    pid = getattr(process, "pid", None)
    if isinstance(pid, bool) or not isinstance(pid, int) or pid <= 0:
        _terminate_owned_process(process)
        raise StartupError(
            f"codex app-server returned an invalid PID {pid!r}; log={paths.log_path}"
        )
    try:
        _write_pid(paths.pid_path, pid)
    except OSError as error:
        _terminate_owned_process(process)
        raise StartupError(
            f"started app-server PID {pid} but could not save owned PID file "
            f"{paths.pid_path}: {error}; the owned child was terminated; "
            f"log={paths.log_path}"
        ) from error

    deadline = monotonic() + timeout_seconds
    last_error = "endpoint did not become ready"
    while True:
        remaining = deadline - monotonic()
        probe_timeout = min(
            DEFAULT_READY_PROBE_TIMEOUT_SECONDS,
            max(remaining, 0.001),
        )
        ready, error = _probe_ready(
            readiness_check,
            paths.endpoint,
            timeout_seconds=probe_timeout,
        )
        if ready:
            return ReadyAppServer(paths=paths, pid=pid, started=True)
        if error:
            last_error = error

        returncode = process.poll()
        if returncode is not None:
            raise StartupError(
                f"repo app-server PID {pid} exited with status {returncode} before "
                f"readiness at {paths.endpoint}: {last_error}; log={paths.log_path}"
            )
        if monotonic() >= deadline:
            raise StartupError(
                f"repo app-server PID {pid} is alive but endpoint {paths.endpoint} "
                f"was not ready within {timeout_seconds:g}s: {last_error}; "
                f"refusing to kill it; pid={paths.pid_path}; log={paths.log_path}"
            )
        sleep(min(poll_seconds, max(deadline - monotonic(), 0.0)))


def _repository_path(value: str | Path | None) -> Path:
    candidate = Path(value) if value is not None else repository_root()
    try:
        resolved = candidate.expanduser().resolve(strict=True)
    except OSError as error:
        raise StartupError(f"repository root is not accessible: {candidate}") from error
    if not resolved.is_dir():
        raise StartupError(f"repository root is not a directory: {resolved}")
    return resolved


def _project_path(value: str | Path) -> Path:
    try:
        path = Path(value).expanduser().resolve(strict=True)
    except OSError as error:
        raise StartupError(f"target project directory is not accessible: {value}") from error
    if not path.is_dir():
        raise StartupError(f"target project is not a directory: {path}")
    if not (path / "README.md").is_file():
        raise StartupError(f"target project root has no README.md: {path}")
    return path


def _resolve_executable(name: str, configured: str | None) -> str:
    executable = configured or shutil.which(name)
    if executable is None:
        raise StartupError(f"required executable is not available on PATH: {name}")
    return executable


def _validate_session_name(session: str) -> None:
    if not session or ":" in session:
        raise StartupError("tmux session name must be non-empty and cannot contain ':'")


def _run_command(command: Sequence[str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        list(command),
        check=False,
        stdin=subprocess.DEVNULL,
        capture_output=True,
        text=True,
        timeout=10,
    )


def _execute(
    runner: CommandRunner,
    command: Sequence[str],
    failure_message: str,
) -> subprocess.CompletedProcess[str]:
    try:
        return runner(tuple(command))
    except (OSError, subprocess.SubprocessError) as error:
        raise StartupError(f"{failure_message}: {error}") from error


def _ensure_runtime_directory(paths: RepoRuntimePaths) -> None:
    expected = paths.repository / RUNTIME_RELATIVE_PATH
    if paths.runtime_dir != expected:
        raise StartupError(
            f"runtime directory is outside the exact repository path: {paths.runtime_dir}"
        )
    internal_root = paths.repository / ".codex-crew"
    for path, label in (
        (internal_root, "repository state root"),
        (paths.runtime_dir, "repository runtime directory"),
    ):
        if path.is_symlink() or (path.exists() and not path.is_dir()):
            raise StartupError(f"{label} is not an owned directory: {path}")
        try:
            path.mkdir(exist_ok=True)
        except OSError as error:
            raise StartupError(f"could not create {label} {path}: {error}") from error
    try:
        resolved_runtime = paths.runtime_dir.resolve(strict=True)
    except OSError as error:
        raise StartupError(
            f"could not resolve repository runtime directory {paths.runtime_dir}: {error}"
        ) from error
    if resolved_runtime != expected:
        raise StartupError(f"runtime directory resolved outside repository: {paths.runtime_dir}")
    expected_artifacts = {
        paths.socket_path: expected / APP_SERVER_SOCKET_NAME,
        paths.pid_path: expected / APP_SERVER_PID_NAME,
        paths.log_path: expected / APP_SERVER_LOG_NAME,
    }
    for path, expected_path in expected_artifacts.items():
        if path != expected_path or path.parent != paths.runtime_dir:
            raise StartupError(f"runtime artifact is not the exact owned path: {path}")
        if path.is_symlink():
            raise StartupError(f"runtime artifact must not be a symlink: {path}")
    artifact_types = (
        (paths.socket_path, stat.S_ISSOCK, "Unix socket"),
        (paths.pid_path, stat.S_ISREG, "regular PID file"),
        (paths.log_path, stat.S_ISREG, "regular log file"),
    )
    for path, predicate, label in artifact_types:
        if not _lexists(path):
            continue
        try:
            mode = path.lstat().st_mode
        except OSError as error:
            raise StartupError(f"could not inspect runtime artifact {path}: {error}") from error
        if not predicate(mode):
            raise StartupError(f"runtime artifact is not an owned {label}: {path}")
    if paths.endpoint != f"unix://{paths.socket_path}":
        raise StartupError(f"runtime endpoint does not match owned socket: {paths.endpoint}")


def _optional_pid(path: Path) -> int | None:
    if not _lexists(path):
        return None
    try:
        mode = path.lstat().st_mode
    except OSError as error:
        raise StartupError(f"cannot inspect app-server PID file {path}: {error}") from error
    if not stat.S_ISREG(mode) or path.is_symlink():
        raise StartupError(f"app-server PID path is not an owned regular file: {path}")
    try:
        text = path.read_text(encoding="ascii").strip()
        pid = int(text)
    except (OSError, UnicodeError, ValueError) as error:
        raise StartupError(f"app-server PID file is invalid: {path}") from error
    if pid <= 0 or str(pid) != text:
        raise StartupError(f"app-server PID file is invalid: {path}")
    return pid


def _process_is_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError as error:
        if error.errno == errno.ESRCH:
            return False
        if error.errno == errno.EPERM:
            return True
        raise StartupError(f"could not inspect app-server PID {pid}: {error}") from error
    return True


def _clean_dead_runtime(paths: RepoRuntimePaths) -> None:
    if _lexists(paths.socket_path):
        try:
            mode = paths.socket_path.lstat().st_mode
        except OSError as error:
            raise StartupError(
                f"cannot inspect stale app-server socket {paths.socket_path}: {error}"
            ) from error
        if not stat.S_ISSOCK(mode):
            raise StartupError(
                f"stale app-server socket path is not an owned Unix socket: "
                f"{paths.socket_path}"
            )
    try:
        if _lexists(paths.socket_path):
            paths.socket_path.unlink()
        paths.pid_path.unlink()
    except OSError as error:
        raise StartupError(
            f"could not clean dead app-server runtime under {paths.runtime_dir}: {error}"
        ) from error


def _validate_log_target(path: Path) -> None:
    if not _lexists(path):
        return
    try:
        mode = path.lstat().st_mode
    except OSError as error:
        raise StartupError(f"cannot inspect app-server log {path}: {error}") from error
    if not stat.S_ISREG(mode) or path.is_symlink():
        raise StartupError(f"app-server log path is not an owned regular file: {path}")


def _write_pid(path: Path, pid: int) -> None:
    temporary_name: str | None = None
    try:
        descriptor, temporary_name = tempfile.mkstemp(
            dir=path.parent,
            prefix=f".{path.name}.",
            text=True,
        )
        with os.fdopen(descriptor, "w", encoding="ascii") as stream:
            stream.write(f"{pid}\n")
        os.link(temporary_name, path)
    finally:
        if temporary_name is not None:
            try:
                Path(temporary_name).unlink()
            except FileNotFoundError:
                pass


def _terminate_owned_process(process: Any) -> None:
    try:
        process.terminate()
    except (AttributeError, OSError, subprocess.SubprocessError):
        pass


def _probe_ready(
    checker: ReadyCheck,
    endpoint: str,
    *,
    timeout_seconds: float,
) -> tuple[bool, str | None]:
    try:
        checker(endpoint, timeout_seconds=timeout_seconds)
    except AppServerError as error:
        return False, str(error)
    return True, None


def _lexists(path: Path) -> bool:
    return os.path.lexists(path)
