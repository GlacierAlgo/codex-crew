"""Codex lifecycle hook handlers."""

from __future__ import annotations

import json
from pathlib import Path
import sys
import time
from typing import Any, IO, Mapping

from codex_crew.storage import StopSnapshot, default_database_path, upsert_snapshot
from codex_crew.transcript import read_transcript_snapshot


def capture_stop(
    payload: Mapping[str, Any],
    *,
    database_path: str | Path | None = None,
    asof_at: int | None = None,
) -> StopSnapshot:
    session_id = _required_string(payload, "session_id")
    turn_id = _required_string(payload, "turn_id")
    transcript = read_transcript_snapshot(
        _optional_string(payload.get("transcript_path"))
    )
    usage = transcript.usage
    goal = transcript.goal

    record = StopSnapshot(
        session_id=session_id,
        turn_id=turn_id,
        asof_at=int(time.time()) if asof_at is None else asof_at,
        model=_optional_string(payload.get("model")),
        final_text=_optional_string(payload.get("last_assistant_message")),
        input_tokens=usage.input_tokens if usage else None,
        cached_input_tokens=usage.cached_input_tokens if usage else None,
        cache_write_input_tokens=usage.cache_write_input_tokens if usage else None,
        output_tokens=usage.output_tokens if usage else None,
        reasoning_output_tokens=usage.reasoning_output_tokens if usage else None,
        goal_objective_excerpt=goal.objective_excerpt if goal else None,
        goal_created_at=goal.created_at if goal else None,
        goal_status=goal.status if goal else None,
        goal_token_budget=goal.token_budget if goal else None,
        goal_tokens_used=goal.tokens_used if goal else None,
        goal_time_used_seconds=goal.time_used_seconds if goal else None,
    )
    resolved_database = (
        Path(database_path).expanduser()
        if database_path
        else default_database_path()
    )
    upsert_snapshot(record, resolved_database)
    return record


def run_stop_hook(
    *,
    database_path: str | Path | None = None,
    stdin: IO[str] = sys.stdin,
    stdout: IO[str] = sys.stdout,
    stderr: IO[str] = sys.stderr,
) -> int:
    """Run fail-open: persistence errors never interrupt the Codex turn."""

    try:
        payload = json.load(stdin)
        if not isinstance(payload, Mapping):
            raise ValueError("Stop hook input must be a JSON object")
        capture_stop(payload, database_path=database_path)
    except Exception as error:  # The hook contract must not break Codex completion.
        print(f"codex-crew: failed to capture Stop snapshot: {error}", file=stderr)

    json.dump({}, stdout, separators=(",", ":"))
    stdout.write("\n")
    return 0


def _required_string(payload: Mapping[str, Any], key: str) -> str:
    value = _optional_string(payload.get(key))
    if value is None:
        raise ValueError(f"Stop hook input is missing {key}")
    return value


def _optional_string(value: Any) -> str | None:
    return value if isinstance(value, str) else None
