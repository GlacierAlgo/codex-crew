"""Launch a manifest-defined Codex crew in one tmux window."""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import asdict, dataclass
from pathlib import Path
import re
import shlex
import shutil
import subprocess
from typing import Any

from codex_crew.loop_package import (
    DEFAULT_LOOP_ID,
    LoopPackageError,
    load_loop_package,
)


class LaunchError(RuntimeError):
    """A launcher precondition or tmux operation failed."""


@dataclass(frozen=True)
class CrewPane:
    role: str
    pane_id: str
    runtime_profile: str
    model: str
    reasoning_effort: str


@dataclass(frozen=True)
class CrewLaunch:
    loop_id: str
    layout: str
    session: str
    window_id: str
    window_index: str
    window_name: str
    project_dir: str
    panes: tuple[CrewPane, ...]

    @property
    def pane_mapping(self) -> dict[str, str]:
        return {pane.role: pane.pane_id for pane in self.panes}

    def as_dict(self) -> dict[str, Any]:
        values = asdict(self)
        values["pane_mapping"] = self.pane_mapping
        return values


CommandRunner = Callable[[Sequence[str]], subprocess.CompletedProcess[str]]


def launch_crew(
    project_dir: str | Path,
    *,
    loop_id: str = DEFAULT_LOOP_ID,
    session: str = "default",
    window_name: str | None = None,
    tmux_executable: str | None = None,
    codex_executable: str | None = None,
    runner: CommandRunner | None = None,
    loops_dir: str | Path | None = None,
) -> CrewLaunch:
    """Create the selected loop's ordered equal-width role columns."""

    try:
        package = load_loop_package(loop_id, loops_dir=loops_dir)
    except LoopPackageError as error:
        raise LaunchError(str(error)) from error
    project = _project_path(project_dir)
    _validate_session_name(session)
    tmux = tmux_executable or _require_executable("tmux")
    codex = codex_executable or _require_executable("codex")
    execute = runner or _run_command

    _checked(
        execute,
        [tmux, "has-session", "-t", f"={session}"],
        f"tmux session {session!r} does not exist",
    )
    for role in package.roles:
        _checked(
            execute,
            [
                codex,
                "--profile",
                role.runtime_profile,
                "--strict-config",
                "--version",
            ],
            f"Codex profile {role.runtime_profile!r} is not loadable",
        )

    name = window_name or _default_window_name(project)
    window_id: str | None = None
    pane_ids: list[str] = []
    try:
        first_role = package.roles[0]
        created = _checked(
            execute,
            [
                tmux,
                "new-window",
                "-d",
                "-P",
                "-F",
                "#{window_id}\t#{pane_id}\t#{window_index}",
                "-t",
                f"={session}:",
                "-n",
                name,
                "-c",
                str(project),
                _codex_command(codex, first_role.runtime_profile, project),
            ],
            "failed to create the crew tmux window",
        )
        window_id, first_pane, window_index = _parse_fields(
            created.stdout, 3, "tmux new-window"
        )
        pane_ids.append(first_pane)

        for role in package.roles[1:]:
            pane_id = _single_field(
                _checked(
                    execute,
                    [
                        tmux,
                        "split-window",
                        "-h",
                        "-d",
                        "-P",
                        "-F",
                        "#{pane_id}",
                        "-t",
                        pane_ids[-1],
                        "-c",
                        str(project),
                        _codex_command(codex, role.runtime_profile, project),
                    ],
                    f"failed to create the {role.id} pane",
                ).stdout,
                f"tmux split-window for {role.id}",
            )
            pane_ids.append(pane_id)

        _checked(
            execute,
            [tmux, "select-layout", "-t", window_id, package.layout.name],
            f"failed to apply tmux layout {package.layout.name!r}",
        )

        window_options = {
            "@codex_crew_loop": package.id,
            "@codex_crew_project": str(project),
        }
        window_options.update(
            {
                f"@codex_{role.id}_pane": pane_id
                for role, pane_id in zip(package.roles, pane_ids, strict=True)
            }
        )
        for key, value in window_options.items():
            _checked(
                execute,
                [tmux, "set-option", "-w", "-t", window_id, key, value],
                f"failed to record tmux window option {key}",
            )
        for role, pane_id in zip(package.roles, pane_ids, strict=True):
            _checked(
                execute,
                [
                    tmux,
                    "set-option",
                    "-p",
                    "-t",
                    pane_id,
                    "@codex_role",
                    role.id,
                ],
                f"failed to record the {role.id} pane role",
            )
    except Exception:
        if window_id is not None:
            _run_ignoring_failure(execute, [tmux, "kill-window", "-t", window_id])
        raise

    return CrewLaunch(
        loop_id=package.id,
        layout=package.layout.name,
        session=session,
        window_id=window_id,
        window_index=window_index,
        window_name=name,
        project_dir=str(project),
        panes=tuple(
            CrewPane(
                role=role.id,
                pane_id=pane_id,
                runtime_profile=role.runtime_profile,
                model=role.model,
                reasoning_effort=role.reasoning_effort,
            )
            for role, pane_id in zip(package.roles, pane_ids, strict=True)
        ),
    )


def _project_path(value: str | Path) -> Path:
    try:
        path = Path(value).expanduser().resolve(strict=True)
    except OSError as error:
        raise LaunchError(f"target project directory is not accessible: {value}") from error
    if not path.is_dir():
        raise LaunchError(f"target project is not a directory: {path}")
    if not (path / "README.md").is_file():
        raise LaunchError(f"target project root has no README.md: {path}")
    return path


def _validate_session_name(session: str) -> None:
    if not session or ":" in session:
        raise LaunchError("tmux session name must be non-empty and cannot contain ':'")


def _require_executable(name: str) -> str:
    executable = shutil.which(name)
    if executable is None:
        raise LaunchError(f"required executable is not available on PATH: {name}")
    return executable


def _default_window_name(project: Path) -> str:
    slug = re.sub(r"[^\w.-]+", "-", project.name, flags=re.UNICODE).strip("-.")
    return f"crew-{slug or 'project'}"


def _codex_command(codex: str, runtime_profile: str, project: Path) -> str:
    return shlex.join(
        [codex, "--profile", runtime_profile, "--strict-config", "-C", str(project)]
    )


def _run_command(command: Sequence[str]) -> subprocess.CompletedProcess[str]:
    try:
        return subprocess.run(
            list(command),
            check=False,
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            timeout=10,
        )
    except (OSError, subprocess.SubprocessError) as error:
        raise LaunchError(f"could not execute {command[0]}: {error}") from error


def _checked(
    runner: CommandRunner,
    command: Sequence[str],
    failure_message: str,
) -> subprocess.CompletedProcess[str]:
    try:
        result = runner(tuple(command))
    except LaunchError:
        raise
    except (OSError, subprocess.SubprocessError) as error:
        raise LaunchError(f"{failure_message}: {error}") from error
    if result.returncode != 0:
        detail = (result.stderr or result.stdout or "").strip()
        suffix = f": {detail}" if detail else ""
        raise LaunchError(f"{failure_message}{suffix}")
    return result


def _run_ignoring_failure(runner: CommandRunner, command: Sequence[str]) -> None:
    try:
        runner(tuple(command))
    except (OSError, subprocess.SubprocessError, LaunchError):
        pass


def _parse_fields(output: str, count: int, operation: str) -> tuple[str, ...]:
    fields = tuple(output.strip().split("\t"))
    if len(fields) != count or any(not field for field in fields):
        raise LaunchError(f"{operation} returned an unexpected result: {output!r}")
    return fields


def _single_field(output: str, operation: str) -> str:
    return _parse_fields(output, 1, operation)[0]
