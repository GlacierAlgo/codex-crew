"""Managed launch ownership records and exact, recoverable crew teardown."""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, replace
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import tempfile
from typing import Any

from codex_crew.app_server import AppServerConnection
from codex_crew.crew_runtime import (
    CrewRuntimeError,
    crew_archive,
    crew_is_archived,
    crew_status,
)
from codex_crew.loop_package import repository_root


LIFECYCLE_SCHEMA_VERSION = 2
LIFECYCLE_KIND = "codex_crew_lifecycle_record"
LIFECYCLE_DIR_NAME = "crew-lifecycle"
_WINDOW_ID_PATTERN = re.compile(r"^@[0-9]+$")
_PANE_ID_PATTERN = re.compile(r"^%[0-9]+$")


class LifecycleError(RuntimeError):
    """A lifecycle record or exact teardown transaction failed closed."""


@dataclass(frozen=True)
class LifecycleRole:
    role: str
    pane_id: str
    thread_id: str
    bootstrap_turn_id: str


@dataclass(frozen=True)
class LifecycleRecord:
    loop_id: str
    project_dir: str
    session: str
    window_id: str
    window_name: str
    window_index: str
    endpoint: str
    communication_role: str
    roles: tuple[LifecycleRole, ...]
    handoff_turn_id: str
    handoff_status: str
    window_reclaim_phase: str = "pending"
    archived_roles: tuple[str, ...] = ()


@dataclass(frozen=True)
class CloseResult:
    window_id: str
    record_path: str
    archived: tuple[tuple[str, str], ...]

    def as_dict(self) -> dict[str, Any]:
        return {
            "schema_version": LIFECYCLE_SCHEMA_VERSION,
            "command": "close",
            "status": "closed",
            "window_id": self.window_id,
            "window_reclaimed": True,
            "record_path": self.record_path,
            "record_removed": True,
            "archived": [
                {"role": role, "thread_id": thread_id}
                for role, thread_id in self.archived
            ],
        }


CommandRunner = Callable[[Sequence[str]], subprocess.CompletedProcess[str]]
ConnectionFactory = Callable[[str], AppServerConnection]
Checkpoint = Callable[[Path, LifecycleRecord], None]


def default_lifecycle_dir() -> Path:
    return repository_root() / ".codex-crew" / "runtime" / LIFECYCLE_DIR_NAME


def external_close_command(window_id: str) -> str:
    _validate_window_id(window_id)
    return f"codex-crew crew close --window-id {window_id}"


def build_lifecycle_record(
    *,
    loop_id: str,
    project_dir: str,
    session: str,
    window_id: str,
    window_name: str,
    window_index: str,
    endpoint: str,
    communication_role: str,
    roles: Sequence[Mapping[str, str]],
    handoff_turn_id: str,
    handoff_status: str,
) -> LifecycleRecord:
    record = LifecycleRecord(
        loop_id=loop_id,
        project_dir=project_dir,
        session=session,
        window_id=window_id,
        window_name=window_name,
        window_index=window_index,
        endpoint=endpoint,
        communication_role=communication_role,
        roles=tuple(
            LifecycleRole(
                role=values["role"],
                pane_id=values["pane_id"],
                thread_id=values["thread_id"],
                bootstrap_turn_id=values["bootstrap_turn_id"],
            )
            for values in roles
        ),
        handoff_turn_id=handoff_turn_id,
        handoff_status=handoff_status,
    )
    _validate_record(record)
    return record


def persist_new_lifecycle_record(
    record: LifecycleRecord,
    *,
    lifecycle_dir: str | Path | None = None,
) -> Path:
    directory = _prepare_lifecycle_dir(lifecycle_dir)
    path = _record_path(record.window_id, directory)
    if path.is_symlink() or path.exists():
        raise LifecycleError(
            f"managed lifecycle record already exists for window {record.window_id}: {path}"
        )
    _atomic_write_record(path, record)
    return path


def close_crew(
    window_id: str,
    *,
    lifecycle_dir: str | Path | None = None,
    tmux_executable: str | None = None,
    runner: CommandRunner | None = None,
    connection_factory: ConnectionFactory = AppServerConnection,
    checkpoint: Checkpoint | None = None,
) -> CloseResult:
    directory = _existing_lifecycle_dir(lifecycle_dir)
    path = _record_path(window_id, directory)
    record = _load_record(path, expected_window_id=window_id)
    archived = set(record.archived_roles)
    save_checkpoint = checkpoint or _checkpoint_record

    active: list[str] = []
    try:
        for role in record.roles:
            if role.role in archived:
                continue
            if crew_is_archived(
                record.endpoint,
                thread_id=role.thread_id,
                connection_factory=connection_factory,
            ):
                continue
            status = crew_status(
                record.endpoint,
                thread_id=role.thread_id,
                connection_factory=connection_factory,
            )
            if status.status == "running":
                active.append(
                    f"role={role.role} thread={role.thread_id} turn={status.turn_id}"
                )
    except CrewRuntimeError as error:
        raise LifecycleError(f"crew close preflight failed: {error}") from error
    if active:
        raise LifecycleError(
            "crew close refused because cohort has active turns: " + "; ".join(active)
        )

    if record.window_reclaim_phase != "complete":
        tmux = tmux_executable or _require_executable("tmux")
        execute = runner or _run_command
        window_exists = True
        if record.window_reclaim_phase == "pending":
            _inspect_live_window(
                record, tmux=tmux, runner=execute, allow_absent=False
            )
            record = replace(record, window_reclaim_phase="started")
            save_checkpoint(path, record)
        else:
            window_exists = _inspect_live_window(
                record, tmux=tmux, runner=execute, allow_absent=True
            )
        if window_exists:
            _checked(
                execute,
                [tmux, "kill-window", "-t", record.window_id],
                f"failed to reclaim exact tmux window {record.window_id}",
            )
        record = replace(record, window_reclaim_phase="complete")
        save_checkpoint(path, record)

    archive_order = tuple(
        role for role in record.roles if role.role != record.communication_role
    ) + tuple(
        role for role in record.roles if role.role == record.communication_role
    )
    for role in archive_order:
        if role.role in archived:
            continue
        try:
            crew_archive(
                record.endpoint,
                thread_id=role.thread_id,
                connection_factory=connection_factory,
            )
        except CrewRuntimeError as error:
            remaining = [
                candidate.role
                for candidate in archive_order
                if candidate.role not in archived
            ]
            raise LifecycleError(
                f"crew close archive failed for role={role.role} "
                f"thread={role.thread_id}: {error}; remaining roles: "
                + ", ".join(remaining)
            ) from error
        archived.add(role.role)
        record = replace(
            record,
            archived_roles=tuple(
                candidate.role
                for candidate in archive_order
                if candidate.role in archived
            ),
        )
        save_checkpoint(path, record)

    archived_mapping = tuple((role.role, role.thread_id) for role in archive_order)
    _load_record(path, expected_window_id=window_id)
    try:
        path.unlink()
    except OSError as error:
        raise LifecycleError(
            f"all cohort threads archived but lifecycle record removal failed: {path}: {error}"
        ) from error
    return CloseResult(
        window_id=window_id,
        record_path=str(path),
        archived=archived_mapping,
    )


def _prepare_lifecycle_dir(value: str | Path | None) -> Path:
    directory = _lifecycle_path(value)
    if directory.is_symlink() or (directory.exists() and not directory.is_dir()):
        raise LifecycleError(f"lifecycle path is not a managed-safe directory: {directory}")
    parent = directory.parent
    if parent.is_symlink() or (parent.exists() and not parent.is_dir()):
        raise LifecycleError(f"lifecycle parent is not a managed-safe directory: {parent}")
    try:
        directory.mkdir(parents=True, exist_ok=True)
    except OSError as error:
        raise LifecycleError(f"cannot create lifecycle directory {directory}: {error}") from error
    return directory


def _existing_lifecycle_dir(value: str | Path | None) -> Path:
    directory = _lifecycle_path(value)
    if directory.is_symlink() or not directory.is_dir():
        raise LifecycleError(f"managed lifecycle directory is unavailable: {directory}")
    return directory


def _lifecycle_path(value: str | Path | None) -> Path:
    return (
        Path(value).expanduser().absolute()
        if value is not None
        else default_lifecycle_dir().absolute()
    )


def _record_path(window_id: str, lifecycle_dir: Path) -> Path:
    _validate_window_id(window_id)
    path = lifecycle_dir / f"window-{window_id[1:]}.json"
    try:
        path.relative_to(lifecycle_dir)
    except ValueError as error:
        raise LifecycleError("lifecycle record path escaped managed directory") from error
    return path


def _validate_window_id(window_id: str) -> None:
    if not isinstance(window_id, str) or not _WINDOW_ID_PATTERN.fullmatch(window_id):
        raise LifecycleError("window id must be an exact tmux ID such as @7")


def _validate_record(record: LifecycleRecord) -> None:
    _validate_window_id(record.window_id)
    for label, value in (
        ("loop_id", record.loop_id),
        ("project_dir", record.project_dir),
        ("session", record.session),
        ("window_name", record.window_name),
        ("window_index", record.window_index),
        ("endpoint", record.endpoint),
        ("communication_role", record.communication_role),
        ("handoff_turn_id", record.handoff_turn_id),
    ):
        if not isinstance(value, str) or not value:
            raise LifecycleError(f"lifecycle record {label} must be non-empty text")
    if record.handoff_status != "completed":
        raise LifecycleError("lifecycle record handoff_status must be completed")
    if not record.roles:
        raise LifecycleError("lifecycle record roles must be non-empty")
    role_ids = [role.role for role in record.roles]
    pane_ids = [role.pane_id for role in record.roles]
    thread_ids = [role.thread_id for role in record.roles]
    bootstrap_ids = [role.bootstrap_turn_id for role in record.roles]
    if any(len(values) != len(set(values)) for values in (role_ids, pane_ids, thread_ids, bootstrap_ids)):
        raise LifecycleError("lifecycle record role mappings must be unique")
    if record.communication_role not in role_ids:
        raise LifecycleError("lifecycle communication role is not in ordered roles")
    for role in record.roles:
        if not all(
            isinstance(value, str) and value
            for value in (
                role.role,
                role.pane_id,
                role.thread_id,
                role.bootstrap_turn_id,
            )
        ):
            raise LifecycleError("lifecycle role mapping contains empty identity")
        if not _PANE_ID_PATTERN.fullmatch(role.pane_id):
            raise LifecycleError(f"invalid exact pane id in lifecycle record: {role.pane_id}")
    if record.window_reclaim_phase not in {"pending", "started", "complete"}:
        raise LifecycleError("lifecycle window_reclaim_phase is invalid")
    if len(record.archived_roles) != len(set(record.archived_roles)) or not set(
        record.archived_roles
    ).issubset(role_ids):
        raise LifecycleError("lifecycle archive progress is invalid")


def _record_document(record: LifecycleRecord) -> dict[str, Any]:
    return {
        "schema_version": LIFECYCLE_SCHEMA_VERSION,
        "kind": LIFECYCLE_KIND,
        "loop_id": record.loop_id,
        "project_dir": record.project_dir,
        "session": record.session,
        "window": {
            "id": record.window_id,
            "name": record.window_name,
            "index": record.window_index,
        },
        "endpoint": record.endpoint,
        "communication_role": record.communication_role,
        "roles": [
            {
                "role": role.role,
                "pane_id": role.pane_id,
                "thread_id": role.thread_id,
                "bootstrap_turn_id": role.bootstrap_turn_id,
            }
            for role in record.roles
        ],
        "handoff": {
            "turn_id": record.handoff_turn_id,
            "status": record.handoff_status,
        },
        "archive_progress": {
            "window_reclaim_phase": record.window_reclaim_phase,
            "archived_roles": list(record.archived_roles),
        },
    }


def _load_record(path: Path, *, expected_window_id: str) -> LifecycleRecord:
    if path.is_symlink() or not path.is_file():
        raise LifecycleError(
            f"unknown or unmanaged lifecycle record for window {expected_window_id}: {path}"
        )
    try:
        document = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as error:
        raise LifecycleError(f"cannot read managed lifecycle record {path}: {error}") from error
    if not isinstance(document, Mapping):
        raise LifecycleError(f"managed lifecycle record is not an object: {path}")
    if document.get("schema_version") != LIFECYCLE_SCHEMA_VERSION or document.get(
        "kind"
    ) != LIFECYCLE_KIND:
        raise LifecycleError(f"unmanaged lifecycle record schema/kind: {path}")
    try:
        window = document["window"]
        handoff = document["handoff"]
        progress = document["archive_progress"]
        raw_roles = document["roles"]
        if not all(
            isinstance(value, Mapping) for value in (window, handoff, progress)
        ) or not isinstance(raw_roles, list):
            raise TypeError("invalid nested record type")
        if not raw_roles or not all(isinstance(role, Mapping) for role in raw_roles):
            raise TypeError("invalid role mapping type")
        archived_roles = progress["archived_roles"]
        if not isinstance(archived_roles, list) or not all(
            isinstance(role, str) for role in archived_roles
        ):
            raise TypeError("invalid archive progress type")
        record = LifecycleRecord(
            loop_id=document["loop_id"],
            project_dir=document["project_dir"],
            session=document["session"],
            window_id=window["id"],
            window_name=window["name"],
            window_index=window["index"],
            endpoint=document["endpoint"],
            communication_role=document["communication_role"],
            roles=tuple(
                LifecycleRole(
                    role=role["role"],
                    pane_id=role["pane_id"],
                    thread_id=role["thread_id"],
                    bootstrap_turn_id=role["bootstrap_turn_id"],
                )
                for role in raw_roles
            ),
            handoff_turn_id=handoff["turn_id"],
            handoff_status=handoff["status"],
            window_reclaim_phase=progress["window_reclaim_phase"],
            archived_roles=tuple(archived_roles),
        )
    except (KeyError, TypeError) as error:
        raise LifecycleError(f"managed lifecycle record is malformed: {path}") from error
    if record.window_id != expected_window_id:
        raise LifecycleError(
            f"lifecycle record window mismatch: {record.window_id} != {expected_window_id}"
        )
    _validate_record(record)
    return record


def _checkpoint_record(path: Path, record: LifecycleRecord) -> None:
    _load_record(path, expected_window_id=record.window_id)
    _atomic_write_record(path, record)


def _atomic_write_record(path: Path, record: LifecycleRecord) -> None:
    _validate_record(record)
    temporary_name: str | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            dir=path.parent,
            prefix=f".{path.name}.",
            delete=False,
        ) as stream:
            json.dump(_record_document(record), stream, ensure_ascii=False, indent=2)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
            temporary_name = stream.name
        os.replace(temporary_name, path)
    except OSError as error:
        raise LifecycleError(f"cannot persist lifecycle record {path}: {error}") from error
    finally:
        if temporary_name is not None:
            try:
                Path(temporary_name).unlink()
            except FileNotFoundError:
                pass


def _inspect_live_window(
    record: LifecycleRecord,
    *,
    tmux: str,
    runner: CommandRunner,
    allow_absent: bool,
) -> bool:
    metadata_result = _run_inspection(
        runner,
        [
            tmux,
            "display-message",
            "-p",
            "-t",
            record.window_id,
            "#{session_name}\t#{window_id}\t#{window_name}\t#{window_index}",
        ],
        f"cannot inspect exact tmux window {record.window_id}",
        window_id=record.window_id,
        allow_absent=allow_absent,
    )
    if metadata_result is None:
        return False
    metadata = metadata_result.stdout.strip().split("\t")
    expected = [
        record.session,
        record.window_id,
        record.window_name,
        record.window_index,
    ]
    if metadata != expected:
        raise LifecycleError(
            f"live tmux window metadata mismatch for {record.window_id}: "
            f"observed={metadata!r} expected={expected!r}"
        )
    panes_result = _run_inspection(
        runner,
        [tmux, "list-panes", "-t", record.window_id, "-F", "#{pane_id}"],
        f"cannot inspect panes for exact tmux window {record.window_id}",
        window_id=record.window_id,
        allow_absent=allow_absent,
    )
    if panes_result is None:
        return False
    panes = panes_result.stdout.splitlines()
    expected_panes = {role.pane_id for role in record.roles}
    if len(panes) != len(expected_panes) or set(panes) != expected_panes:
        raise LifecycleError(
            f"live tmux pane set mismatch for {record.window_id}: "
            f"observed={sorted(panes)!r} expected={sorted(expected_panes)!r}"
        )
    return True


def _run_inspection(
    runner: CommandRunner,
    command: Sequence[str],
    failure_message: str,
    *,
    window_id: str,
    allow_absent: bool,
) -> subprocess.CompletedProcess[str] | None:
    try:
        result = runner(tuple(command))
    except (OSError, subprocess.SubprocessError, LifecycleError) as error:
        raise LifecycleError(f"{failure_message}: {error}") from error
    if result.returncode == 0:
        return result
    detail = (result.stderr or result.stdout or "").strip()
    if allow_absent and _exact_window_absent(detail, window_id):
        return None
    suffix = f": {detail}" if detail else ""
    raise LifecycleError(f"{failure_message}{suffix}")


def _exact_window_absent(detail: str, window_id: str) -> bool:
    return detail in {
        f"can't find window: {window_id}",
        f"no such window: {window_id}",
    }


def _require_executable(name: str) -> str:
    executable = shutil.which(name)
    if executable is None:
        raise LifecycleError(f"required executable is not available on PATH: {name}")
    return executable


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
        raise LifecycleError(f"could not execute {command[0]}: {error}") from error


def _checked(
    runner: CommandRunner,
    command: Sequence[str],
    failure_message: str,
) -> subprocess.CompletedProcess[str]:
    try:
        result = runner(tuple(command))
    except (OSError, subprocess.SubprocessError, LifecycleError) as error:
        raise LifecycleError(f"{failure_message}: {error}") from error
    if result.returncode != 0:
        detail = (result.stderr or result.stdout or "").strip()
        suffix = f": {detail}" if detail else ""
        raise LifecycleError(f"{failure_message}{suffix}")
    return result
