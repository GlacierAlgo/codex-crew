from __future__ import annotations

import copy
import unittest

from codex_crew.app_server import (
    AppServerAmbiguousRequestError,
    AppServerError,
    AppServerNotification,
    AppServerRequestUnavailableError,
)
from codex_crew.crew_runtime import (
    CrewRuntimeError,
    crew_archive,
    crew_final,
    crew_goal_clear,
    crew_goal_get,
    crew_goal_set,
    crew_send,
    crew_status,
    crew_steer,
    crew_wait,
)


ENDPOINT = "unix:///tmp/native.sock"
THREAD_ID = "thread-worker"


def _user_item(text: str) -> dict:
    return {
        "type": "userMessage",
        "content": [{"type": "text", "text": text}],
    }


def _final_item(text: str) -> dict:
    return {
        "type": "agentMessage",
        "phase": "final_answer",
        "text": text,
    }


class FakeState:
    def __init__(self) -> None:
        self.thread = {
            "id": THREAD_ID,
            "sessionId": THREAD_ID,
            "status": {"type": "idle"},
            "turns": [
                {
                    "id": "bootstrap",
                    "status": "completed",
                    "items": [_user_item("role=worker"), _final_item("role=worker")],
                }
            ],
        }
        self.requests: list[tuple[str, dict]] = []
        self.notifications: list[AppServerNotification] = []
        self.goal: dict | None = None
        self.unavailable_count = 0
        self.ambiguous_once = False
        self.disconnect_on_notification = False
        self.archived = False

    def active(self, turn_id: str, message: str = "task") -> dict:
        turn = {
            "id": turn_id,
            "status": "inProgress",
            "items": [_user_item(message)],
        }
        self.thread["turns"].append(turn)
        self.thread["status"] = {"type": "active"}
        return turn

    def complete(self, turn: dict, final: str) -> None:
        turn["status"] = "completed"
        turn["items"].append(_final_item(final))
        self.thread["status"] = {"type": "idle"}


class FakeConnection:
    def __init__(self, state: FakeState, endpoint: str) -> None:
        if endpoint != ENDPOINT:
            raise AppServerError(f"wrong endpoint {endpoint}")
        self.state = state

    def __enter__(self):
        return self

    def __exit__(self, *args) -> None:
        return None

    def request(self, method: str, params: dict | None = None):
        values = copy.deepcopy(params or {})
        self.state.requests.append((method, values))
        if method == "thread/read":
            return {"thread": copy.deepcopy(self.state.thread)}
        if method == "thread/list":
            return {
                "data": ([{"id": THREAD_ID}] if self.state.archived else []),
                "nextCursor": None,
            }
        if method == "thread/archive":
            self.state.archived = True
            return None
        if method == "thread/resume":
            return {"thread": {"id": THREAD_ID, "sessionId": THREAD_ID}}
        if method == "turn/start":
            if self.state.unavailable_count:
                self.state.unavailable_count -= 1
                raise AppServerRequestUnavailableError(
                    request_id=1,
                    method=method,
                    code=-32001,
                    message="busy",
                )
            turn = self.state.active(
                f"task-{len(self.state.thread['turns'])}",
                params["input"][0]["text"],
            )
            if self.state.ambiguous_once:
                self.state.ambiguous_once = False
                raise AppServerAmbiguousRequestError(
                    request_id=2,
                    method=method,
                    reason="fixture disconnect",
                )
            return {"turn": copy.deepcopy(turn)}
        if method == "turn/steer":
            return {"turnId": params["expectedTurnId"]}
        if method == "thread/goal/get":
            return {"goal": copy.deepcopy(self.state.goal)}
        if method == "thread/goal/set":
            self.state.goal = {
                "threadId": THREAD_ID,
                "objective": params["objective"],
                "status": params["status"],
                "tokenBudget": params.get("tokenBudget"),
            }
            return {"goal": copy.deepcopy(self.state.goal)}
        if method == "thread/goal/clear":
            self.state.goal = None
            return {}
        raise AssertionError((method, params))

    def receive_notification(self, **kwargs) -> AppServerNotification:
        if self.state.disconnect_on_notification:
            raise AppServerError("fixture disconnect while waiting")
        if not self.state.notifications:
            raise AppServerError("timed out waiting for an app-server notification")
        notification = self.state.notifications.pop(0)
        if notification.method == "turn/completed":
            completed = notification.params["turn"]
            persisted = next(
                turn
                for turn in self.state.thread["turns"]
                if turn["id"] == completed["id"]
            )
            persisted.clear()
            persisted.update(copy.deepcopy(completed))
            self.state.thread["status"] = {"type": "idle"}
        return notification


class CrewRuntimeTests(unittest.TestCase):
    def setUp(self) -> None:
        self.state = FakeState()
        self.factory = lambda endpoint: FakeConnection(self.state, endpoint)

    def test_status_and_send_use_only_endpoint_and_native_thread_id(self) -> None:
        status = crew_status(
            ENDPOINT, thread_id=THREAD_ID, connection_factory=self.factory
        )
        self.assertEqual("completed", status.status)
        self.assertEqual("role=worker", status.data["final_text"])

        result = crew_send(
            ENDPOINT,
            thread_id=THREAD_ID,
            message="line one\nline two\n",
            connection_factory=self.factory,
        )
        self.assertEqual("running", result.status)
        start = next(
            params for method, params in self.state.requests if method == "turn/start"
        )
        self.assertEqual(
            {
                "threadId": THREAD_ID,
                "input": [{"type": "text", "text": "line one\nline two\n"}],
            },
            start,
        )
        self.assertNotIn("thread/resume", [method for method, _ in self.state.requests])

    def test_active_send_fails_and_steer_requires_exact_turn(self) -> None:
        self.state.active("active-turn")
        with self.assertRaisesRegex(CrewRuntimeError, "use crew steer"):
            crew_send(
                ENDPOINT,
                thread_id=THREAD_ID,
                message="duplicate",
                connection_factory=self.factory,
            )
        with self.assertRaisesRegex(CrewRuntimeError, "stale expected"):
            crew_steer(
                ENDPOINT,
                thread_id=THREAD_ID,
                expected_turn_id="stale",
                message="more",
                connection_factory=self.factory,
            )
        result = crew_steer(
            ENDPOINT,
            thread_id=THREAD_ID,
            expected_turn_id="active-turn",
            message="more",
            connection_factory=self.factory,
        )
        self.assertEqual("active-turn", result.turn_id)

    def test_retry_and_ambiguous_dispatch_reconciliation_are_bounded(self) -> None:
        self.state.unavailable_count = 2
        sleeps: list[float] = []
        result = crew_send(
            ENDPOINT,
            thread_id=THREAD_ID,
            message="retry",
            connection_factory=self.factory,
            sleep=sleeps.append,
            random_unit=lambda: 0.0,
        )
        self.assertEqual([0.1, 0.2], sleeps)
        self.assertEqual("running", result.status)

        other = FakeState()
        other.ambiguous_once = True
        factory = lambda endpoint: FakeConnection(other, endpoint)
        reconciled = crew_send(
            ENDPOINT,
            thread_id=THREAD_ID,
            message="ambiguous exact text",
            connection_factory=factory,
        )
        self.assertEqual("running", reconciled.status)
        self.assertEqual(
            1,
            sum(method == "turn/start" for method, _ in other.requests),
        )

    def test_wait_reconciles_completion_and_uses_native_events(self) -> None:
        active = self.state.active("turn-after", "work")
        completed = copy.deepcopy(active)
        completed["status"] = "completed"
        final = _final_item("authoritative final")
        completed["items"].append(final)
        usage = {"total": {"inputTokens": 11, "outputTokens": 7}}
        self.state.notifications = [
            AppServerNotification(
                "thread/tokenUsage/updated",
                {
                    "threadId": THREAD_ID,
                    "turnId": "turn-after",
                    "tokenUsage": usage,
                },
            ),
            AppServerNotification(
                "item/completed",
                {
                    "threadId": THREAD_ID,
                    "turnId": "turn-after",
                    "item": final,
                },
            ),
            AppServerNotification(
                "turn/completed",
                {"threadId": THREAD_ID, "turn": completed},
            ),
        ]
        result = crew_wait(
            ENDPOINT,
            thread_id=THREAD_ID,
            turn_id="turn-after",
            timeout_seconds=1,
            connection_factory=self.factory,
        )
        self.assertEqual("authoritative final", result.data["final_text"])
        self.assertEqual(usage, result.data["token_usage"])
        resumes = [
            params for method, params in self.state.requests if method == "thread/resume"
        ]
        self.assertEqual([{"threadId": THREAD_ID}], resumes)

        repeated = crew_final(
            ENDPOINT,
            thread_id=THREAD_ID,
            turn_id="turn-after",
            connection_factory=self.factory,
        )
        self.assertEqual("authoritative final", repeated.data["final_text"])

    def test_usage_after_completion_is_not_fabricated_by_wait_status_or_final(self) -> None:
        baseline = crew_status(
            ENDPOINT, thread_id=THREAD_ID, connection_factory=self.factory
        )
        active = self.state.active("turn-late-usage", "work")
        completed = copy.deepcopy(active)
        completed["status"] = "completed"
        completed["items"].append(_final_item("done without observed usage"))
        late_usage = {"total": {"inputTokens": 23, "outputTokens": 5}}
        self.state.notifications = [
            AppServerNotification(
                "turn/completed",
                {"threadId": THREAD_ID, "turn": completed},
            ),
            AppServerNotification(
                "thread/tokenUsage/updated",
                {
                    "threadId": THREAD_ID,
                    "turnId": "turn-late-usage",
                    "tokenUsage": late_usage,
                },
            ),
        ]

        waited = crew_wait(
            ENDPOINT,
            thread_id=THREAD_ID,
            turn_id="turn-late-usage",
            timeout_seconds=1,
            connection_factory=self.factory,
        )
        status = crew_status(
            ENDPOINT, thread_id=THREAD_ID, connection_factory=self.factory
        )
        final = crew_final(
            ENDPOINT,
            thread_id=THREAD_ID,
            turn_id="turn-late-usage",
            connection_factory=self.factory,
        )

        self.assertIsNone(baseline.data["token_usage"])
        self.assertIsNone(waited.data["token_usage"])
        self.assertIsNone(status.data["token_usage"])
        self.assertIsNone(final.data["token_usage"])
        self.assertEqual("thread/tokenUsage/updated", self.state.notifications[0].method)

    def test_goal_commands_are_native_and_do_not_require_binding_state(self) -> None:
        set_result = crew_goal_set(
            ENDPOINT,
            thread_id=THREAD_ID,
            objective="finish migration",
            token_budget=5000,
            connection_factory=self.factory,
        )
        self.assertEqual(5000, set_result.data["goal"]["tokenBudget"])
        get_result = crew_goal_get(
            ENDPOINT, thread_id=THREAD_ID, connection_factory=self.factory
        )
        self.assertEqual("finish migration", get_result.data["goal"]["objective"])
        clear_result = crew_goal_clear(
            ENDPOINT, thread_id=THREAD_ID, connection_factory=self.factory
        )
        self.assertIsNone(clear_result.data["goal"])

    def test_archive_accepts_empty_response_and_reconciles_archived_listing(self) -> None:
        first = crew_archive(
            ENDPOINT, thread_id=THREAD_ID, connection_factory=self.factory
        )
        second = crew_archive(
            ENDPOINT, thread_id=THREAD_ID, connection_factory=self.factory
        )

        self.assertEqual("archived", first.status)
        self.assertFalse(first.data["reconciled"])
        self.assertTrue(second.data["reconciled"])
        self.assertEqual(
            1,
            sum(method == "thread/archive" for method, _ in self.state.requests),
        )

    def test_wrong_thread_and_wait_disconnect_fail_closed(self) -> None:
        with self.assertRaisesRegex(CrewRuntimeError, "not a path/window/record locator"):
            crew_status(
                ENDPOINT,
                thread_id="/repo/runtime/window-7.json",
                connection_factory=self.factory,
            )

        def wrong_factory(endpoint: str):
            connection = FakeConnection(self.state, endpoint)
            self.state.thread["id"] = "wrong"
            return connection

        with self.assertRaisesRegex(CrewRuntimeError, "wrong thread id"):
            crew_status(
                ENDPOINT, thread_id=THREAD_ID, connection_factory=wrong_factory
            )

        self.state = FakeState()
        self.factory = lambda endpoint: FakeConnection(self.state, endpoint)
        self.state.active("turn-timeout")
        self.state.disconnect_on_notification = True
        with self.assertRaisesRegex(CrewRuntimeError, "fixture disconnect"):
            crew_wait(
                ENDPOINT,
                thread_id=THREAD_ID,
                turn_id="turn-timeout",
                timeout_seconds=0.1,
                connection_factory=self.factory,
            )


if __name__ == "__main__":
    unittest.main()
