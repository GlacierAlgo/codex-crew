"""App-server-native control for one exact Codex thread."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass
import random
import time
from typing import Any

from codex_crew.app_server import (
    AppServerAmbiguousRequestError,
    AppServerConnection,
    AppServerError,
    AppServerRequestUnavailableError,
)


SCHEMA_VERSION = 2
DEFAULT_WAIT_TIMEOUT_SECONDS = 120.0
DEFAULT_DISPATCH_ATTEMPTS = 3
DEFAULT_RETRY_BASE_SECONDS = 0.1
DEFAULT_RETRY_JITTER_SECONDS = 0.05
MAX_MESSAGE_BYTES = 1024 * 1024
MAX_GOAL_OBJECTIVE_CODE_POINTS = 4000
_TERMINAL_TURN_STATUSES = frozenset({"completed", "failed", "interrupted"})


class CrewRuntimeError(RuntimeError):
    """A native thread operation failed closed."""


@dataclass(frozen=True)
class CrewCommandResult:
    command: str
    endpoint: str
    thread_id: str
    turn_id: str | None
    status: str
    data: Mapping[str, Any]

    def as_dict(self) -> dict[str, Any]:
        return {
            "schema_version": SCHEMA_VERSION,
            "command": self.command,
            "endpoint": self.endpoint,
            "thread_id": self.thread_id,
            "turn_id": self.turn_id,
            "status": self.status,
            "data": dict(self.data),
        }


ConnectionFactory = Callable[[str], AppServerConnection]
Sleep = Callable[[float], None]
RandomUnit = Callable[[], float]


def crew_status(
    endpoint: str,
    *,
    thread_id: str,
    connection_factory: ConnectionFactory = AppServerConnection,
) -> CrewCommandResult:
    """Read one exact thread without resuming or mutating it."""

    _validate_thread_id(thread_id)
    try:
        with connection_factory(endpoint) as connection:
            thread = _read_thread(connection, thread_id)
            active = _active_turn(thread)
            if active is not None:
                return _result(
                    "status",
                    endpoint,
                    thread_id,
                    turn_id=active["id"],
                    status="running",
                    data={"thread_status": "active"},
                )
            latest = _latest_turn(thread)
            if latest is not None and latest.get("status") in _TERMINAL_TURN_STATUSES:
                return _terminal_result("status", endpoint, thread, latest)
            return _result(
                "status",
                endpoint,
                thread_id,
                turn_id=None,
                status="idle",
                data={"thread_status": _thread_status_type(thread)},
            )
    except CrewRuntimeError:
        raise
    except AppServerError as error:
        raise CrewRuntimeError(str(error)) from error


def crew_send(
    endpoint: str,
    *,
    thread_id: str,
    message: str,
    connection_factory: ConnectionFactory = AppServerConnection,
    sleep: Sleep = time.sleep,
    random_unit: RandomUnit = random.random,
    attempts: int = DEFAULT_DISPATCH_ATTEMPTS,
) -> CrewCommandResult:
    """Start one turn, with bounded retry and ambiguous-outcome reconciliation."""

    _validate_thread_id(thread_id)
    _validate_message(message)
    if attempts < 1:
        raise CrewRuntimeError("dispatch attempts must be positive")

    before_turn_ids: set[str] = set()
    dispatch_started = False
    try:
        with connection_factory(endpoint) as connection:
            before = _read_thread(connection, thread_id)
            active = _active_turn(before)
            if active is not None:
                raise CrewRuntimeError(
                    f"thread {thread_id} already has active turn {active['id']}; "
                    f"use crew steer with --expected-turn-id {active['id']}"
                )
            before_turn_ids = _turn_ids(before)
            params = {
                "threadId": thread_id,
                "input": [{"type": "text", "text": message}],
            }
            dispatch_started = True
            response: Any = None
            for attempt in range(1, attempts + 1):
                try:
                    response = connection.request("turn/start", params)
                    break
                except AppServerRequestUnavailableError:
                    if attempt == attempts:
                        raise
                    delay = DEFAULT_RETRY_BASE_SECONDS * (2 ** (attempt - 1))
                    delay += DEFAULT_RETRY_JITTER_SECONDS * random_unit()
                    sleep(delay)
            turn = _turn_result(response, "turn/start")
            return _started_turn_result(
                "send", endpoint, thread_id, connection, turn
            )
    except AppServerRequestUnavailableError as error:
        raise CrewRuntimeError(
            f"turn/start was rejected with -32001 after {attempts} attempts: {error}"
        ) from error
    except AppServerAmbiguousRequestError as error:
        if not dispatch_started or error.method != "turn/start":
            raise CrewRuntimeError(str(error)) from error
        return _reconcile_dispatch(
            endpoint,
            thread_id,
            message,
            before_turn_ids,
            connection_factory=connection_factory,
            cause=error,
        )
    except CrewRuntimeError:
        raise
    except AppServerError as error:
        raise CrewRuntimeError(str(error)) from error


def crew_steer(
    endpoint: str,
    *,
    thread_id: str,
    expected_turn_id: str,
    message: str,
    connection_factory: ConnectionFactory = AppServerConnection,
) -> CrewCommandResult:
    """Append one complete input to the exact authoritative active turn."""

    _validate_thread_id(thread_id)
    _validate_turn_id(expected_turn_id, "expected turn id")
    _validate_message(message)
    try:
        with connection_factory(endpoint) as connection:
            thread = _read_thread(connection, thread_id)
            _validate_expected_active_turn(thread, expected_turn_id)
            response = connection.request(
                "turn/steer",
                {
                    "threadId": thread_id,
                    "expectedTurnId": expected_turn_id,
                    "input": [{"type": "text", "text": message}],
                },
            )
            returned_id = response.get("turnId") if isinstance(response, dict) else None
            if returned_id != expected_turn_id:
                raise CrewRuntimeError(
                    f"turn/steer returned unexpected turnId {returned_id!r}; "
                    f"expected {expected_turn_id!r}"
                )
            return _result(
                "steer",
                endpoint,
                thread_id,
                turn_id=expected_turn_id,
                status="running",
                data={"steered": True},
            )
    except CrewRuntimeError:
        raise
    except AppServerError as error:
        raise CrewRuntimeError(str(error)) from error


def crew_wait(
    endpoint: str,
    *,
    thread_id: str,
    turn_id: str,
    timeout_seconds: float = DEFAULT_WAIT_TIMEOUT_SECONDS,
    connection_factory: ConnectionFactory = AppServerConnection,
) -> CrewCommandResult:
    """Wait for one exact turn using native events and completion reconciliation."""

    _validate_thread_id(thread_id)
    _validate_turn_id(turn_id, "turn id")
    if timeout_seconds <= 0:
        raise CrewRuntimeError("wait timeout must be greater than zero")
    deadline = time.monotonic() + timeout_seconds

    try:
        with connection_factory(endpoint) as connection:
            thread = _read_thread(connection, thread_id)
            turn = _require_turn(thread, turn_id)
            if turn.get("status") in _TERMINAL_TURN_STATUSES:
                return _terminal_result("wait", endpoint, thread, turn)

            # thread/read is non-subscribing. Resume only this controller
            # connection so it receives the existing thread's events; no
            # role/profile/config override is replayed.
            _resume_for_events(connection, thread_id)
            thread = _read_thread(connection, thread_id)
            turn = _require_turn(thread, turn_id)
            if turn.get("status") in _TERMINAL_TURN_STATUSES:
                return _terminal_result("wait", endpoint, thread, turn)
            _validate_expected_active_turn(thread, turn_id)

            latest_usage: Mapping[str, Any] | None = None
            event_final: str | None = None
            while True:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise CrewRuntimeError(
                        _wait_timeout_message(endpoint, thread_id, turn_id)
                    )
                notification = connection.receive_notification(
                    timeout_seconds=remaining
                )
                params = notification.params
                if not isinstance(params, dict) or params.get("threadId") != thread_id:
                    continue
                if notification.method == "thread/tokenUsage/updated":
                    if params.get("turnId") == turn_id and isinstance(
                        params.get("tokenUsage"), Mapping
                    ):
                        latest_usage = params["tokenUsage"]
                    continue
                if notification.method == "item/completed":
                    if params.get("turnId") == turn_id:
                        final = _final_from_item(params.get("item"))
                        if final is not None:
                            event_final = final
                    continue
                if notification.method == "turn/completed":
                    completed = params.get("turn")
                    if isinstance(completed, Mapping) and completed.get("id") == turn_id:
                        thread = _read_thread(connection, thread_id)
                        persisted = _find_turn(thread, turn_id) or dict(completed)
                        return _terminal_result(
                            "wait",
                            endpoint,
                            thread,
                            persisted,
                            token_usage=latest_usage,
                            event_final=event_final,
                        )
                    continue
                if notification.method == "thread/status/changed":
                    status = params.get("status")
                    status_type = status.get("type") if isinstance(status, dict) else None
                    if status_type == "systemError":
                        raise CrewRuntimeError(
                            f"thread {thread_id} entered systemError while waiting"
                        )
                    if status_type in {"idle", "notLoaded"}:
                        thread = _read_thread(connection, thread_id)
                        persisted = _find_turn(thread, turn_id)
                        if (
                            persisted is not None
                            and persisted.get("status") in _TERMINAL_TURN_STATUSES
                        ):
                            return _terminal_result(
                                "wait",
                                endpoint,
                                thread,
                                persisted,
                                token_usage=latest_usage,
                                event_final=event_final,
                            )
    except CrewRuntimeError:
        raise
    except AppServerError as error:
        if "timed out waiting" in str(error):
            raise CrewRuntimeError(
                _wait_timeout_message(endpoint, thread_id, turn_id)
            ) from error
        raise CrewRuntimeError(str(error)) from error


def crew_final(
    endpoint: str,
    *,
    thread_id: str,
    turn_id: str | None,
    connection_factory: ConnectionFactory = AppServerConnection,
) -> CrewCommandResult:
    """Return the authoritative final agentMessage for one terminal turn."""

    _validate_thread_id(thread_id)
    if turn_id is not None:
        _validate_turn_id(turn_id, "turn id")
    try:
        with connection_factory(endpoint) as connection:
            thread = _read_thread(connection, thread_id)
            selected = (
                _find_turn(thread, turn_id)
                if turn_id is not None
                else _latest_turn(thread)
            )
            if selected is None:
                label = turn_id if turn_id is not None else "latest turn"
                raise CrewRuntimeError(f"thread {thread_id} has no {label!r}")
            if selected.get("status") not in _TERMINAL_TURN_STATUSES:
                raise CrewRuntimeError(
                    f"turn {selected.get('id')!r} is not terminal: "
                    f"status={selected.get('status')!r}"
                )
            result = _terminal_result("final", endpoint, thread, selected)
            if result.status != "completed":
                raise CrewRuntimeError(
                    f"turn {result.turn_id!r} ended with status {result.status!r}"
                )
            return result
    except CrewRuntimeError:
        raise
    except AppServerError as error:
        raise CrewRuntimeError(str(error)) from error


def crew_goal_get(
    endpoint: str,
    *,
    thread_id: str,
    connection_factory: ConnectionFactory = AppServerConnection,
) -> CrewCommandResult:
    return _crew_goal_command(
        endpoint,
        thread_id=thread_id,
        command="goal.get",
        method="thread/goal/get",
        params={},
        connection_factory=connection_factory,
    )


def crew_goal_set(
    endpoint: str,
    *,
    thread_id: str,
    objective: str,
    token_budget: int | None,
    connection_factory: ConnectionFactory = AppServerConnection,
) -> CrewCommandResult:
    if not objective.strip():
        raise CrewRuntimeError("goal objective must be non-empty")
    if len(objective) > MAX_GOAL_OBJECTIVE_CODE_POINTS:
        raise CrewRuntimeError("goal objective exceeds 4000 Unicode characters")
    if token_budget is not None and token_budget <= 0:
        raise CrewRuntimeError("goal token budget must be positive")
    params: dict[str, Any] = {"objective": objective, "status": "active"}
    if token_budget is not None:
        params["tokenBudget"] = token_budget
    return _crew_goal_command(
        endpoint,
        thread_id=thread_id,
        command="goal.set",
        method="thread/goal/set",
        params=params,
        connection_factory=connection_factory,
    )


def crew_goal_clear(
    endpoint: str,
    *,
    thread_id: str,
    connection_factory: ConnectionFactory = AppServerConnection,
) -> CrewCommandResult:
    return _crew_goal_command(
        endpoint,
        thread_id=thread_id,
        command="goal.clear",
        method="thread/goal/clear",
        params={},
        connection_factory=connection_factory,
    )


def _crew_goal_command(
    endpoint: str,
    *,
    thread_id: str,
    command: str,
    method: str,
    params: Mapping[str, Any],
    connection_factory: ConnectionFactory,
) -> CrewCommandResult:
    _validate_thread_id(thread_id)
    try:
        with connection_factory(endpoint) as connection:
            response = connection.request(
                method, {"threadId": thread_id, **params}
            )
    except AppServerError as error:
        raise CrewRuntimeError(str(error)) from error
    if not isinstance(response, dict):
        raise CrewRuntimeError(f"{method} returned malformed response data")
    goal = response.get("goal")
    if method != "thread/goal/clear" and "goal" not in response:
        raise CrewRuntimeError(f"{method} returned no goal field")
    if goal is not None and not isinstance(goal, dict):
        raise CrewRuntimeError(f"{method} returned malformed goal data")
    return _result(
        command,
        endpoint,
        thread_id,
        turn_id=None,
        status="ok",
        data={"goal": goal},
    )


def _read_thread(
    connection: AppServerConnection, thread_id: str
) -> dict[str, Any]:
    response = connection.request(
        "thread/read", {"threadId": thread_id, "includeTurns": True}
    )
    if not isinstance(response, dict) or not isinstance(response.get("thread"), dict):
        raise CrewRuntimeError("thread/read returned no thread object")
    thread = response["thread"]
    if thread.get("id") != thread_id:
        raise CrewRuntimeError(
            f"thread/read returned wrong thread id {thread.get('id')!r}; "
            f"expected {thread_id!r}"
        )
    if not isinstance(thread.get("turns"), list):
        raise CrewRuntimeError("thread/read(includeTurns=true) returned no turns list")
    status = thread.get("status")
    if status is not None:
        if not isinstance(status, dict) or status.get("type") not in {
            "notLoaded",
            "idle",
            "systemError",
            "active",
        }:
            raise CrewRuntimeError(f"thread/read returned malformed status {status!r}")
        if status.get("type") == "systemError":
            raise CrewRuntimeError(f"thread {thread_id} is in systemError state")
    _active_turn(thread)
    return thread


def _resume_for_events(
    connection: AppServerConnection, thread_id: str
) -> None:
    response = connection.request("thread/resume", {"threadId": thread_id})
    if not isinstance(response, dict) or not isinstance(response.get("thread"), dict):
        raise CrewRuntimeError("thread/resume returned no thread object")
    if response["thread"].get("id") != thread_id:
        raise CrewRuntimeError(
            f"thread/resume returned wrong thread id "
            f"{response['thread'].get('id')!r}; expected {thread_id!r}"
        )


def _active_turn(thread: Mapping[str, Any]) -> dict[str, Any] | None:
    active = [
        turn
        for turn in thread.get("turns", [])
        if isinstance(turn, dict) and turn.get("status") == "inProgress"
    ]
    if len(active) > 1:
        raise CrewRuntimeError(
            f"thread {thread.get('id')!r} has multiple active turns: "
            + ", ".join(str(turn.get("id")) for turn in active)
        )
    if active and (not isinstance(active[0].get("id"), str) or not active[0]["id"]):
        raise CrewRuntimeError("active turn has no valid id")
    status = thread.get("status")
    if isinstance(status, dict) and status.get("type") == "active" and not active:
        raise CrewRuntimeError(
            f"thread {thread.get('id')!r} reports active without an inProgress turn"
        )
    return active[0] if active else None


def _validate_expected_active_turn(
    thread: Mapping[str, Any], expected_turn_id: str
) -> None:
    active = _active_turn(thread)
    if active is None:
        raise CrewRuntimeError(
            f"thread {thread.get('id')} has no active turn; expected {expected_turn_id!r}"
        )
    if active["id"] != expected_turn_id:
        raise CrewRuntimeError(
            f"stale expected turn id {expected_turn_id!r}; authoritative active turn "
            f"is {active['id']!r}"
        )


def _started_turn_result(
    command: str,
    endpoint: str,
    thread_id: str,
    connection: AppServerConnection,
    turn: dict[str, Any],
) -> CrewCommandResult:
    turn_id = turn["id"]
    status = turn.get("status")
    if status == "inProgress":
        return _result(
            command,
            endpoint,
            thread_id,
            turn_id=turn_id,
            status="running",
            data={"dispatched": True},
        )
    if status in _TERMINAL_TURN_STATUSES:
        thread = _read_thread(connection, thread_id)
        persisted = _find_turn(thread, turn_id) or turn
        return _terminal_result(command, endpoint, thread, persisted)
    raise CrewRuntimeError(
        f"turn/start returned unsupported turn status {status!r} for {turn_id!r}"
    )


def _reconcile_dispatch(
    endpoint: str,
    thread_id: str,
    message: str,
    before_turn_ids: set[str],
    *,
    connection_factory: ConnectionFactory,
    cause: Exception,
) -> CrewCommandResult:
    matching: list[dict[str, Any]] = []
    try:
        with connection_factory(endpoint) as connection:
            thread = _read_thread(connection, thread_id)
            matching = [
                turn
                for turn in thread["turns"]
                if isinstance(turn, dict)
                and turn.get("id") not in before_turn_ids
                and _turn_has_exact_user_text(turn, message)
            ]
            if len(matching) == 1:
                return _started_turn_result(
                    "send", endpoint, thread_id, connection, matching[0]
                )
    except Exception as reconcile_error:
        detail = f"; reconciliation failed: {reconcile_error}"
    else:
        detail = f"; matching new turns={len(matching)}"
    raise CrewRuntimeError(
        "dispatch_unknown: turn/start outcome is ambiguous and was not retried; "
        f"endpoint={endpoint} thread={thread_id} cause={cause}{detail}"
    ) from cause


def _terminal_result(
    command: str,
    endpoint: str,
    thread: Mapping[str, Any],
    turn: Mapping[str, Any],
    *,
    token_usage: Mapping[str, Any] | None = None,
    event_final: str | None = None,
) -> CrewCommandResult:
    thread_id = thread.get("id")
    turn_id = turn.get("id")
    status = turn.get("status")
    if not isinstance(thread_id, str) or not isinstance(turn_id, str):
        raise CrewRuntimeError("terminal result has invalid thread or turn id")
    if status not in _TERMINAL_TURN_STATUSES:
        raise CrewRuntimeError("terminal result received a non-terminal turn")
    if status != "completed":
        return _result(
            command,
            endpoint,
            thread_id,
            turn_id=turn_id,
            status=status,
            data={"final_text": None, "error": turn.get("error")},
        )
    final_text = _final_from_turn(turn) or event_final
    if final_text is None:
        raise CrewRuntimeError(
            f"completed turn {turn_id!r} has no authoritative final agentMessage"
        )
    usage = token_usage or _token_usage_from_state(thread, turn)
    return _result(
        command,
        endpoint,
        thread_id,
        turn_id=turn_id,
        status="completed",
        data={
            "final_text": final_text,
            "token_usage": dict(usage) if usage is not None else None,
        },
    )


def _result(
    command: str,
    endpoint: str,
    thread_id: str,
    *,
    turn_id: str | None,
    status: str,
    data: Mapping[str, Any],
) -> CrewCommandResult:
    return CrewCommandResult(
        command=command,
        endpoint=endpoint,
        thread_id=thread_id,
        turn_id=turn_id,
        status=status,
        data=data,
    )


def _turn_result(response: Any, method: str) -> dict[str, Any]:
    if not isinstance(response, dict) or not isinstance(response.get("turn"), dict):
        raise CrewRuntimeError(f"{method} returned no turn object")
    turn = response["turn"]
    if not isinstance(turn.get("id"), str) or not turn["id"]:
        raise CrewRuntimeError(f"{method} returned invalid turn.id")
    return turn


def _turn_ids(thread: Mapping[str, Any]) -> set[str]:
    return {
        turn["id"]
        for turn in thread.get("turns", [])
        if isinstance(turn, dict) and isinstance(turn.get("id"), str)
    }


def _find_turn(
    thread: Mapping[str, Any], turn_id: str | None
) -> dict[str, Any] | None:
    if turn_id is None:
        return None
    matches = [
        turn
        for turn in thread.get("turns", [])
        if isinstance(turn, dict) and turn.get("id") == turn_id
    ]
    if len(matches) > 1:
        raise CrewRuntimeError(f"thread contains duplicate turn id {turn_id!r}")
    return matches[0] if matches else None


def _require_turn(thread: Mapping[str, Any], turn_id: str) -> dict[str, Any]:
    turn = _find_turn(thread, turn_id)
    if turn is None:
        raise CrewRuntimeError(
            f"thread {thread.get('id')} has no turn {turn_id!r}"
        )
    return turn


def _latest_turn(thread: Mapping[str, Any]) -> dict[str, Any] | None:
    turns = [turn for turn in thread.get("turns", []) if isinstance(turn, dict)]
    return turns[-1] if turns else None


def _turn_has_exact_user_text(turn: Mapping[str, Any], message: str) -> bool:
    matches: list[Any] = []
    for item in turn.get("items", []):
        if not isinstance(item, dict) or item.get("type") != "userMessage":
            continue
        for content in item.get("content", []):
            if isinstance(content, dict) and content.get("type") == "text":
                matches.append(content.get("text"))
    return matches.count(message) == 1


def _final_from_turn(turn: Mapping[str, Any]) -> str | None:
    finals = [
        text
        for item in turn.get("items", [])
        if (text := _final_from_item(item)) is not None
    ]
    return finals[-1] if finals else None


def _final_from_item(item: Any) -> str | None:
    if not isinstance(item, Mapping) or item.get("type") != "agentMessage":
        return None
    if item.get("phase") not in {None, "final_answer"}:
        return None
    return item.get("text") if isinstance(item.get("text"), str) else None


def _token_usage_from_state(
    thread: Mapping[str, Any], turn: Mapping[str, Any]
) -> Mapping[str, Any] | None:
    for source in (turn, thread):
        usage = source.get("tokenUsage")
        if isinstance(usage, Mapping):
            return usage
    return None


def _thread_status_type(thread: Mapping[str, Any]) -> str:
    status = thread.get("status")
    return status.get("type", "idle") if isinstance(status, dict) else "idle"


def _validate_thread_id(thread_id: str) -> None:
    _validate_turn_id(thread_id, "thread id")


def _validate_turn_id(value: str, label: str) -> None:
    if not isinstance(value, str) or not value.strip():
        raise CrewRuntimeError(f"{label} must be non-empty")


def _validate_message(message: str) -> None:
    if not isinstance(message, str):
        raise CrewRuntimeError("message must be text")
    if not message:
        raise CrewRuntimeError("message must be non-empty")
    if len(message.encode("utf-8")) > MAX_MESSAGE_BYTES:
        raise CrewRuntimeError(f"message exceeds {MAX_MESSAGE_BYTES} UTF-8 bytes")


def _wait_timeout_message(endpoint: str, thread_id: str, turn_id: str) -> str:
    return (
        "wait timed out; "
        f"endpoint={endpoint} thread={thread_id} turn={turn_id}; "
        "retry the same crew wait command"
    )
