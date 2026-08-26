"""Best-effort extraction from Codex JSONL transcripts.

The transcript is deliberately treated as an unstable compatibility boundary.
Unknown and malformed records are ignored so the Stop hook can remain fail-open.
"""

from __future__ import annotations

from dataclasses import dataclass
import json
from pathlib import Path
from typing import Any, Mapping


@dataclass(frozen=True)
class UsageSnapshot:
    input_tokens: int | None = None
    cached_input_tokens: int | None = None
    cache_write_input_tokens: int | None = None
    output_tokens: int | None = None
    reasoning_output_tokens: int | None = None


@dataclass(frozen=True)
class GoalSnapshot:
    objective_excerpt: str | None = None
    created_at: int | None = None
    status: str | None = None
    token_budget: int | None = None
    tokens_used: int | None = None
    time_used_seconds: int | None = None


@dataclass(frozen=True)
class TranscriptSnapshot:
    usage: UsageSnapshot | None = None
    goal: GoalSnapshot | None = None


def shorten_goal_objective(value: str | None) -> str | None:
    """Keep an objective intact through 40 Unicode code points, else 20...20."""

    if value is None:
        return None
    characters = list(value)
    if len(characters) <= 40:
        return value
    return "".join(characters[:20]) + "..." + "".join(characters[-20:])


def read_transcript_snapshot(path: str | Path | None) -> TranscriptSnapshot:
    if not path:
        return TranscriptSnapshot()

    transcript_path = Path(path).expanduser()
    latest_usage: UsageSnapshot | None = None
    latest_goal: GoalSnapshot | None = None

    try:
        lines = transcript_path.open("r", encoding="utf-8")
    except OSError:
        return TranscriptSnapshot()

    with lines:
        for line in lines:
            try:
                document = json.loads(line)
            except (json.JSONDecodeError, TypeError):
                continue
            if not isinstance(document, Mapping):
                continue

            event_type, payload = _event(document)
            if event_type in {"token_count", "thread/tokenUsage/updated"}:
                usage = _usage_from(event_type, payload)
                if usage is not None:
                    latest_usage = usage
            elif event_type in {"thread_goal_updated", "thread/goal/updated"}:
                goal = _goal_from(payload)
                if goal is not None:
                    latest_goal = goal
            elif event_type in {"thread_goal_cleared", "thread/goal/cleared"}:
                latest_goal = None

    return TranscriptSnapshot(usage=latest_usage, goal=latest_goal)


def _event(document: Mapping[str, Any]) -> tuple[str | None, Mapping[str, Any]]:
    if document.get("type") == "event_msg" and isinstance(document.get("payload"), Mapping):
        payload = document["payload"]
        event_type = payload.get("type")
        return (event_type if isinstance(event_type, str) else None, payload)

    method = document.get("method")
    params = document.get("params")
    if isinstance(method, str) and isinstance(params, Mapping):
        return method, params

    return None, {}


def _usage_from(event_type: str, payload: Mapping[str, Any]) -> UsageSnapshot | None:
    if event_type == "token_count":
        info = _mapping(payload.get("info"))
        total = _mapping(_first(info, "total_token_usage", "totalTokenUsage"))
    else:
        token_usage = _mapping(_first(payload, "tokenUsage", "token_usage"))
        total = _mapping(token_usage.get("total"))

    if not total:
        return None

    return UsageSnapshot(
        input_tokens=_nonnegative_int(_first(total, "input_tokens", "inputTokens")),
        cached_input_tokens=_nonnegative_int(
            _first(total, "cached_input_tokens", "cachedInputTokens")
        ),
        cache_write_input_tokens=_nonnegative_int(
            _first(total, "cache_write_input_tokens", "cacheWriteInputTokens")
        ),
        output_tokens=_nonnegative_int(_first(total, "output_tokens", "outputTokens")),
        reasoning_output_tokens=_nonnegative_int(
            _first(total, "reasoning_output_tokens", "reasoningOutputTokens")
        ),
    )


def _goal_from(payload: Mapping[str, Any]) -> GoalSnapshot | None:
    goal = _mapping(payload.get("goal"))
    if not goal:
        return None

    objective = goal.get("objective")
    return GoalSnapshot(
        objective_excerpt=shorten_goal_objective(
            objective if isinstance(objective, str) else None
        ),
        created_at=_nonnegative_int(_first(goal, "createdAt", "created_at")),
        status=goal.get("status") if isinstance(goal.get("status"), str) else None,
        token_budget=_nonnegative_int(_first(goal, "tokenBudget", "token_budget")),
        tokens_used=_nonnegative_int(_first(goal, "tokensUsed", "tokens_used")),
        time_used_seconds=_nonnegative_int(
            _first(goal, "timeUsedSeconds", "time_used_seconds")
        ),
    )


def _mapping(value: Any) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


def _first(values: Mapping[str, Any], *keys: str) -> Any:
    for key in keys:
        if key in values:
            return values[key]
    return None


def _nonnegative_int(value: Any) -> int | None:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        return None
    return value
