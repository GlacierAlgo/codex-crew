"""Launch a manifest-defined native-thread Codex crew in one tmux window."""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import asdict, dataclass
import json
from pathlib import Path
import re
import shlex
import shutil
import subprocess
import time
from typing import Any
import uuid

from codex_crew.app_server import (
    DEFAULT_APP_SERVER_ENDPOINT,
    AppServerConnection,
    AppServerError,
)
from codex_crew.loop_package import (
    DEFAULT_LOOP_ID,
    EvenHorizontalTmuxLayout,
    LoopPackageError,
    LoopRole,
    SplitPlanTmuxLayout,
    load_loop_package,
)
from codex_crew.crew_runtime import (
    CrewRuntimeError,
    crew_final,
    crew_send,
    crew_wait,
)
from codex_crew.lifecycle import (
    LifecycleError,
    build_lifecycle_record,
    external_close_command,
    persist_new_lifecycle_record,
)


DEFAULT_DISCOVERY_TIMEOUT_SECONDS = 120.0
DEFAULT_HANDOFF_TIMEOUT_SECONDS = 120.0
DEFAULT_DISCOVERY_POLL_SECONDS = 0.1
DEFAULT_DISCOVERY_PAGE_LIMIT = 100
DEFAULT_DISCOVERY_MAX_PAGES = 20
INTERACTIVE_SOURCE_KINDS = ("cli", "vscode")


class LaunchError(RuntimeError):
    """A launcher precondition, discovery, or tmux operation failed."""


@dataclass(frozen=True)
class CrewPane:
    role: str
    pane_id: str
    runtime_profile: str
    model: str
    reasoning_effort: str
    service_tier: str
    bootstrap_marker: str
    thread_id: str
    bootstrap_turn_id: str


@dataclass(frozen=True)
class CrewLaunch:
    loop_id: str
    layout: str
    app_server_endpoint: str
    session: str
    window_id: str
    window_index: str
    window_name: str
    project_dir: str
    panes: tuple[CrewPane, ...]
    communication_role: str
    handoff_turn_id: str
    handoff_status: str
    lifecycle_record_path: str
    close_command: str

    @property
    def pane_mapping(self) -> dict[str, str]:
        return {pane.role: pane.pane_id for pane in self.panes}

    @property
    def thread_mapping(self) -> dict[str, str]:
        return {pane.role: pane.thread_id for pane in self.panes}

    @property
    def communication_pane_id(self) -> str:
        return self._communication_pane().pane_id

    @property
    def communication_thread_id(self) -> str:
        return self._communication_pane().thread_id

    def _communication_pane(self) -> CrewPane:
        return next(pane for pane in self.panes if pane.role == self.communication_role)

    def as_dict(self) -> dict[str, Any]:
        values = asdict(self)
        values["pane_mapping"] = self.pane_mapping
        values["thread_mapping"] = self.thread_mapping
        values["communication_pane_id"] = self.communication_pane_id
        values["communication_thread_id"] = self.communication_thread_id
        return values


CommandRunner = Callable[[Sequence[str]], subprocess.CompletedProcess[str]]
ConnectionFactory = Callable[[str], AppServerConnection]
Sleep = Callable[[float], None]
Monotonic = Callable[[], float]
MarkerFactory = Callable[[], str]


def launch_crew(
    project_dir: str | Path,
    *,
    loop_id: str = DEFAULT_LOOP_ID,
    session: str = "default",
    window_name: str | None = None,
    app_server_endpoint: str = DEFAULT_APP_SERVER_ENDPOINT,
    tmux_executable: str | None = None,
    codex_executable: str | None = None,
    runner: CommandRunner | None = None,
    loops_dir: str | Path | None = None,
    connection_factory: ConnectionFactory = AppServerConnection,
    discovery_timeout_seconds: float = DEFAULT_DISCOVERY_TIMEOUT_SECONDS,
    discovery_poll_seconds: float = DEFAULT_DISCOVERY_POLL_SECONDS,
    handoff_timeout_seconds: float = DEFAULT_HANDOFF_TIMEOUT_SECONDS,
    lifecycle_dir: str | Path | None = None,
    sleep: Sleep = time.sleep,
    monotonic: Monotonic = time.monotonic,
    marker_factory: MarkerFactory = lambda: uuid.uuid4().hex,
) -> CrewLaunch:
    """Create ordered panes and launch one fresh profiled TUI thread per role."""

    try:
        package = load_loop_package(loop_id, loops_dir=loops_dir)
    except LoopPackageError as error:
        raise LaunchError(str(error)) from error
    project = _project_path(project_dir)
    _validate_session_name(session)
    if discovery_timeout_seconds <= 0:
        raise LaunchError("discovery timeout must be greater than zero")
    if discovery_poll_seconds <= 0:
        raise LaunchError("discovery poll interval must be greater than zero")
    if handoff_timeout_seconds <= 0:
        raise LaunchError("handoff timeout must be greater than zero")
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

    try:
        before_thread_ids = _list_thread_ids(
            app_server_endpoint,
            cwd=str(project),
            connection_factory=connection_factory,
        )
    except (AppServerError, LaunchError) as error:
        raise LaunchError(f"app-server discovery preflight failed: {error}") from error

    launch_marker = marker_factory()
    if not launch_marker or any(character.isspace() for character in launch_marker):
        raise LaunchError("bootstrap marker factory returned an invalid marker")
    markers = {
        role.id: f"CODEX_CREW_BOOTSTRAP:{launch_marker}:role={role.id}"
        for role in package.roles
    }
    prompts = {
        role.id: _bootstrap_prompt(role, markers[role.id])
        for role in package.roles
    }

    name = window_name or _default_window_name(package.id, project)
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
                _codex_command(
                    codex,
                    first_role.runtime_profile,
                    project,
                    app_server_endpoint=app_server_endpoint,
                    bootstrap_prompt=prompts[first_role.id],
                ),
            ],
            "failed to create the crew tmux window",
        )
        window_id, first_pane, window_index = _parse_fields(
            created.stdout, 3, "tmux new-window"
        )
        pane_ids.append(first_pane)

        pane_by_role = {first_role.id: first_pane}
        if isinstance(package.layout, EvenHorizontalTmuxLayout):
            split_specs = (
                (role, pane_ids[-1], "-h", None)
                for role in package.roles[1:]
            )
        elif isinstance(package.layout, SplitPlanTmuxLayout):
            split_specs = (
                (
                    role,
                    pane_by_role[step.target],
                    "-h" if step.direction == "horizontal" else "-v",
                    step.percentage,
                )
                for role, step in zip(
                    package.roles[1:], package.layout.steps, strict=True
                )
            )
        else:  # pragma: no cover - parser constructs the closed layout union.
            raise LaunchError(f"unsupported tmux layout {package.layout!r}")

        for role, target_pane, direction, percentage in split_specs:
            split_command = [
                tmux,
                "split-window",
                direction,
            ]
            if percentage is not None:
                split_command.extend(("-p", str(percentage)))
            split_command.extend(
                (
                    "-d",
                    "-P",
                    "-F",
                    "#{pane_id}",
                    "-t",
                    target_pane,
                    "-c",
                    str(project),
                    _codex_command(
                        codex,
                        role.runtime_profile,
                        project,
                        app_server_endpoint=app_server_endpoint,
                        bootstrap_prompt=prompts[role.id],
                    ),
                )
            )
            pane_id = _single_field(
                _checked(
                    execute,
                    split_command,
                    f"failed to create the {role.id} pane",
                ).stdout,
                f"tmux split-window for {role.id}",
            )
            pane_ids.append(pane_id)
            pane_by_role[role.id] = pane_id

        if isinstance(package.layout, EvenHorizontalTmuxLayout):
            _checked(
                execute,
                [tmux, "select-layout", "-t", window_id, package.layout.name],
                f"failed to apply tmux layout {package.layout.name!r}",
            )
    except Exception as error:
        if window_id is not None:
            _run_ignoring_failure(execute, [tmux, "kill-window", "-t", window_id])
        if isinstance(error, LaunchError):
            raise
        raise LaunchError(str(error)) from error

    try:
        discovered = _discover_bootstrap_threads(
            app_server_endpoint,
            cwd=str(project),
            roles=tuple(role.id for role in package.roles),
            markers=markers,
            before_thread_ids=before_thread_ids,
            timeout_seconds=discovery_timeout_seconds,
            poll_seconds=discovery_poll_seconds,
            connection_factory=connection_factory,
            sleep=sleep,
            monotonic=monotonic,
        )
        missing = [role.id for role in package.roles if role.id not in discovered]
        if missing:
            raise LaunchError(
                "bootstrap discovery deadline expired; missing roles: "
                + ", ".join(missing)
            )
    except (AppServerError, LaunchError) as error:
        raise LaunchError(
            f"crew window {window_id} bootstrap discovery failed: {error}; "
            "window preserved for diagnosis"
        ) from error

    panes = tuple(
        CrewPane(
            role=role.id,
            pane_id=pane_id,
            runtime_profile=role.runtime_profile,
            model=role.model,
            reasoning_effort=role.reasoning_effort,
            service_tier=role.service_tier,
            bootstrap_marker=markers[role.id],
            thread_id=discovered[role.id][0],
            bootstrap_turn_id=discovered[role.id][1],
        )
        for role, pane_id in zip(package.roles, pane_ids, strict=True)
    )
    communication_pane = next(
        pane for pane in panes if pane.role == package.communication_role
    )
    close_command = external_close_command(window_id)
    handoff_message = _runtime_handoff_message(
        loop_id=package.id,
        project_dir=str(project),
        session=session,
        window_id=window_id,
        window_index=window_index,
        window_name=name,
        endpoint=app_server_endpoint,
        communication_role=package.communication_role,
        panes=panes,
        close_command=close_command,
    )
    try:
        handoff_turn_id, handoff_status = _complete_runtime_handoff(
            app_server_endpoint,
            thread_id=communication_pane.thread_id,
            message=handoff_message,
            timeout_seconds=handoff_timeout_seconds,
            connection_factory=connection_factory,
        )
    except CrewRuntimeError as error:
        raise LaunchError(
            f"crew window {window_id} communication handoff failed for role "
            f"{package.communication_role!r} thread {communication_pane.thread_id!r}: "
            f"{error}; window preserved for diagnosis"
        ) from error

    lifecycle_record = build_lifecycle_record(
        loop_id=package.id,
        project_dir=str(project),
        session=session,
        window_id=window_id,
        window_index=window_index,
        window_name=name,
        endpoint=app_server_endpoint,
        communication_role=package.communication_role,
        roles=[
            {
                "role": pane.role,
                "pane_id": pane.pane_id,
                "thread_id": pane.thread_id,
                "bootstrap_turn_id": pane.bootstrap_turn_id,
            }
            for pane in panes
        ],
        handoff_turn_id=handoff_turn_id,
        handoff_status=handoff_status,
    )
    try:
        lifecycle_record_path = persist_new_lifecycle_record(
            lifecycle_record, lifecycle_dir=lifecycle_dir
        )
    except LifecycleError as error:
        raise LaunchError(
            f"crew window {window_id} lifecycle record persist failed: {error}; "
            "window preserved for diagnosis"
        ) from error

    return CrewLaunch(
        loop_id=package.id,
        layout=package.layout.name,
        app_server_endpoint=app_server_endpoint,
        session=session,
        window_id=window_id,
        window_index=window_index,
        window_name=name,
        project_dir=str(project),
        panes=panes,
        communication_role=package.communication_role,
        handoff_turn_id=handoff_turn_id,
        handoff_status=handoff_status,
        lifecycle_record_path=str(lifecycle_record_path),
        close_command=close_command,
    )


def _runtime_handoff_message(
    *,
    loop_id: str,
    project_dir: str,
    session: str,
    window_id: str,
    window_index: str,
    window_name: str,
    endpoint: str,
    communication_role: str,
    panes: tuple[CrewPane, ...],
    close_command: str,
) -> str:
    envelope = {
        "schema_version": 1,
        "kind": "codex_crew_runtime_handoff",
        "loop_id": loop_id,
        "project_dir": project_dir,
        "session": session,
        "window": {
            "id": window_id,
            "index": window_index,
            "name": window_name,
        },
        "endpoint": endpoint,
        "communication_role": communication_role,
        "external_close_command": close_command,
        "roles": [
            {
                "role": pane.role,
                "pane_id": pane.pane_id,
                "thread_id": pane.thread_id,
                "bootstrap_turn_id": pane.bootstrap_turn_id,
            }
            for pane in panes
        ],
    }
    return "CODEX_CREW_RUNTIME_HANDOFF\n" + json.dumps(
        envelope, ensure_ascii=False, indent=2
    )


def _complete_runtime_handoff(
    endpoint: str,
    *,
    thread_id: str,
    message: str,
    timeout_seconds: float,
    connection_factory: ConnectionFactory,
) -> tuple[str, str]:
    started = crew_send(
        endpoint,
        thread_id=thread_id,
        message=message,
        connection_factory=connection_factory,
    )
    if started.turn_id is None:
        raise CrewRuntimeError("communication handoff dispatch returned no turn id")
    waited = crew_wait(
        endpoint,
        thread_id=thread_id,
        turn_id=started.turn_id,
        timeout_seconds=timeout_seconds,
        connection_factory=connection_factory,
    )
    if waited.status != "completed":
        raise CrewRuntimeError(
            f"communication handoff turn {started.turn_id!r} ended with "
            f"status {waited.status!r}"
        )
    final = crew_final(
        endpoint,
        thread_id=thread_id,
        turn_id=started.turn_id,
        connection_factory=connection_factory,
    )
    final_text = final.data.get("final_text")
    first_line = (
        final_text.splitlines()[0]
        if isinstance(final_text, str) and final_text.splitlines()
        else ""
    )
    expected = "runtime_handoff=ready"
    if first_line != expected:
        raise CrewRuntimeError(
            f"communication handoff turn {started.turn_id!r} declared first line "
            f"{first_line!r}; expected {expected!r}"
        )
    return started.turn_id, final.status


def _discover_bootstrap_threads(
    endpoint: str,
    *,
    cwd: str,
    roles: tuple[str, ...],
    markers: Mapping[str, str],
    before_thread_ids: set[str],
    timeout_seconds: float,
    poll_seconds: float,
    connection_factory: ConnectionFactory,
    sleep: Sleep,
    monotonic: Monotonic,
) -> dict[str, tuple[str, str]]:
    deadline = monotonic() + timeout_seconds
    discovered: dict[str, tuple[str, str]] = {}
    while True:
        with connection_factory(endpoint) as connection:
            summaries = _list_threads(connection, cwd=cwd)
            candidates = [
                summary
                for summary in summaries
                if summary["id"] not in before_thread_ids
            ]
            matches: dict[str, list[tuple[str, str]]] = {
                role: [] for role in roles
            }
            for summary in candidates:
                thread_id = summary["id"]
                thread = _read_discovery_thread(connection, thread_id)
                thread_matches = []
                for role in roles:
                    turn_id = _bootstrap_turn_id(
                        thread, markers[role], role=role
                    )
                    if turn_id is not None:
                        thread_matches.append((role, turn_id))
                if len(thread_matches) > 1:
                    raise LaunchError(
                        f"thread {thread_id!r} matches multiple bootstrap roles"
                    )
                if thread_matches:
                    role, turn_id = thread_matches[0]
                    matches[role].append((thread_id, turn_id))
            for role, role_matches in matches.items():
                unique = list(dict.fromkeys(role_matches))
                if len(unique) > 1:
                    raise LaunchError(
                        f"bootstrap marker for role {role!r} matched multiple new threads"
                    )
                if unique:
                    discovered[role] = unique[0]
        if len(discovered) == len(roles) or monotonic() >= deadline:
            return discovered
        sleep(min(poll_seconds, max(deadline - monotonic(), 0.0)))


def _list_thread_ids(
    endpoint: str,
    *,
    cwd: str,
    connection_factory: ConnectionFactory,
) -> set[str]:
    with connection_factory(endpoint) as connection:
        return {summary["id"] for summary in _list_threads(connection, cwd=cwd)}


def _list_threads(
    connection: AppServerConnection,
    *,
    cwd: str,
) -> list[dict[str, Any]]:
    cursor: str | None = None
    seen_cursors: set[str] = set()
    output: list[dict[str, Any]] = []
    for _ in range(DEFAULT_DISCOVERY_MAX_PAGES):
        params: dict[str, Any] = {
            "limit": DEFAULT_DISCOVERY_PAGE_LIMIT,
            "sortKey": "created_at",
            "sortDirection": "desc",
            "sourceKinds": list(INTERACTIVE_SOURCE_KINDS),
            "cwd": cwd,
            "archived": False,
        }
        if cursor is not None:
            params["cursor"] = cursor
        response = connection.request("thread/list", params)
        if not isinstance(response, dict) or not isinstance(response.get("data"), list):
            raise LaunchError("thread/list returned malformed data")
        for summary in response["data"]:
            if not isinstance(summary, dict):
                raise LaunchError("thread/list returned a malformed thread summary")
            thread_id = summary.get("id")
            if not isinstance(thread_id, str) or not thread_id:
                raise LaunchError("thread/list returned a thread without a valid id")
            output.append(summary)
        next_cursor = response.get("nextCursor")
        if next_cursor is None:
            return output
        if not isinstance(next_cursor, str) or not next_cursor:
            raise LaunchError("thread/list returned an invalid nextCursor")
        if next_cursor in seen_cursors:
            raise LaunchError("thread/list repeated a pagination cursor")
        seen_cursors.add(next_cursor)
        cursor = next_cursor
    raise LaunchError(
        "thread/list exceeded the bounded discovery pagination limit"
    )


def _read_discovery_thread(
    connection: AppServerConnection, thread_id: str
) -> dict[str, Any]:
    response = connection.request(
        "thread/read", {"threadId": thread_id, "includeTurns": True}
    )
    if not isinstance(response, dict) or not isinstance(response.get("thread"), dict):
        raise LaunchError("thread/read returned no thread object during discovery")
    thread = response["thread"]
    if thread.get("id") != thread_id:
        raise LaunchError(
            f"thread/read returned {thread.get('id')!r}; expected {thread_id!r}"
        )
    if not isinstance(thread.get("turns"), list):
        raise LaunchError("thread/read returned no turns during discovery")
    return thread


def _bootstrap_turn_id(
    thread: Mapping[str, Any], marker: str, *, role: str
) -> str | None:
    matching: list[Mapping[str, Any]] = []
    for turn in thread.get("turns", []):
        if not isinstance(turn, dict) or not isinstance(turn.get("id"), str):
            continue
        for item in turn.get("items", []):
            if not isinstance(item, dict) or item.get("type") != "userMessage":
                continue
            for content in item.get("content", []):
                if not isinstance(content, dict) or content.get("type") != "text":
                    continue
                text = content.get("text")
                if not isinstance(text, str):
                    continue
                lines = set(text.splitlines())
                if marker in lines and f"role={role}" in lines:
                    matching.append(turn)
    unique = {
        turn["id"]: turn
        for turn in matching
    }
    if len(unique) > 1:
        raise LaunchError(
            f"thread {thread.get('id')!r} contains duplicate bootstrap marker {marker!r}"
        )
    if not unique:
        return None

    turn_id, turn = next(iter(unique.items()))
    status = turn.get("status")
    if status == "inProgress":
        return None
    if status in {"failed", "interrupted"}:
        raise LaunchError(
            f"bootstrap turn {turn_id!r} for role {role!r} ended with "
            f"status {status!r}"
        )
    if status != "completed":
        raise LaunchError(
            f"bootstrap turn {turn_id!r} for role {role!r} has invalid "
            f"status {status!r}"
        )

    finals: list[str] = []
    for item in turn.get("items", []):
        if not isinstance(item, Mapping):
            continue
        if item.get("type") != "agentMessage" or item.get("phase") != "final_answer":
            continue
        text = item.get("text")
        if isinstance(text, str):
            finals.append(text)
    if not finals:
        raise LaunchError(
            f"completed bootstrap turn {turn_id!r} for role {role!r} has no "
            "authoritative final_answer agentMessage"
        )
    first_line = finals[-1].splitlines()[0] if finals[-1].splitlines() else ""
    expected = f"role={role}"
    if first_line != expected:
        raise LaunchError(
            f"completed bootstrap turn {turn_id!r} for role {role!r} declared "
            f"first line {first_line!r}; expected {expected!r}"
        )
    return turn_id


def _bootstrap_prompt(role: LoopRole, marker: str) -> str:
    return "\n".join(
        (
            marker,
            f"role={role.id}",
            "这是本 Codex thread 的可见 identity bootstrap turn。",
            "请读取已加载的 role profile，确认身份与职责；本 turn 不要修改 worktree。",
            f"最终回答第一行必须严格为 role={role.id}。",
        )
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


def _default_window_name(loop_id: str, project: Path) -> str:
    slug = re.sub(r"[^\w.-]+", "-", project.name, flags=re.UNICODE).strip("-.")
    return f"crew-{loop_id}-{slug or 'project'}"


def _codex_command(
    codex: str,
    runtime_profile: str,
    project: Path,
    *,
    app_server_endpoint: str,
    bootstrap_prompt: str,
) -> str:
    return shlex.join(
        [
            codex,
            "--profile",
            runtime_profile,
            "--strict-config",
            "--yolo",
            "--remote",
            app_server_endpoint,
            "-C",
            str(project),
            bootstrap_prompt,
        ]
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
