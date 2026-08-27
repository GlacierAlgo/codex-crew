"""SQLite persistence for cumulative Stop-time snapshots."""

from __future__ import annotations

from contextlib import closing
from dataclasses import asdict, dataclass
import os
from pathlib import Path
import sqlite3
from typing import Any


SCHEMA = """
CREATE TABLE IF NOT EXISTS turn_stop_snapshot (
    session_id               TEXT    NOT NULL,
    turn_id                  TEXT    NOT NULL,
    asof_at                  INTEGER NOT NULL,
    model                    TEXT,
    final_text               TEXT,

    input_tokens             INTEGER CHECK (input_tokens >= 0),
    cached_input_tokens      INTEGER CHECK (cached_input_tokens >= 0),
    cache_write_input_tokens INTEGER CHECK (cache_write_input_tokens >= 0),
    output_tokens            INTEGER CHECK (output_tokens >= 0),
    reasoning_output_tokens  INTEGER CHECK (reasoning_output_tokens >= 0),

    goal_objective_excerpt   TEXT,
    goal_created_at          INTEGER CHECK (goal_created_at >= 0),
    goal_status              TEXT,
    goal_token_budget        INTEGER CHECK (goal_token_budget >= 0),
    goal_tokens_used         INTEGER CHECK (goal_tokens_used >= 0),
    goal_time_used_seconds   INTEGER CHECK (goal_time_used_seconds >= 0),

    PRIMARY KEY (session_id, turn_id)
) STRICT;

CREATE INDEX IF NOT EXISTS idx_turn_stop_snapshot_latest
    ON turn_stop_snapshot (session_id, asof_at DESC);
"""


@dataclass(frozen=True)
class StopSnapshot:
    session_id: str
    turn_id: str
    asof_at: int
    model: str | None = None
    final_text: str | None = None
    input_tokens: int | None = None
    cached_input_tokens: int | None = None
    cache_write_input_tokens: int | None = None
    output_tokens: int | None = None
    reasoning_output_tokens: int | None = None
    goal_objective_excerpt: str | None = None
    goal_created_at: int | None = None
    goal_status: str | None = None
    goal_token_budget: int | None = None
    goal_tokens_used: int | None = None
    goal_time_used_seconds: int | None = None


def default_database_path() -> Path:
    configured = os.environ.get("CODEX_CREW_DB")
    if configured:
        return Path(configured).expanduser()
    state_home = os.environ.get("XDG_STATE_HOME")
    root = (
        Path(state_home).expanduser()
        if state_home
        else Path.home() / ".local" / "state"
    )
    return root / "codex-crew" / "snapshots.sqlite3"


def initialize_database(path: str | Path | None = None) -> Path:
    database_path = _path(path)
    database_path.parent.mkdir(parents=True, exist_ok=True)
    with closing(connect(database_path)) as connection:
        with connection:
            connection.executescript(SCHEMA)
    try:
        database_path.chmod(0o600)
    except OSError:
        pass
    return database_path


def connect(path: str | Path | None = None) -> sqlite3.Connection:
    database_path = _path(path)
    database_path.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(database_path, timeout=5.0)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA busy_timeout = 5000")
    connection.execute("PRAGMA journal_mode = WAL")
    return connection


def upsert_snapshot(snapshot: StopSnapshot, path: str | Path | None = None) -> None:
    initialize_database(path)
    values = asdict(snapshot)
    columns = tuple(values)
    placeholders = ", ".join(f":{column}" for column in columns)
    updates = ", ".join(
        f"{column} = excluded.{column}"
        for column in columns
        if column not in {"session_id", "turn_id"}
    )
    sql = f"""
        INSERT INTO turn_stop_snapshot ({", ".join(columns)})
        VALUES ({placeholders})
        ON CONFLICT (session_id, turn_id) DO UPDATE SET {updates}
    """
    with closing(connect(path)) as connection:
        with connection:
            connection.execute(sql, values)


def latest_snapshots(
    path: str | Path | None = None,
    *,
    session_id: str | None = None,
    limit: int = 20,
) -> list[dict[str, Any]]:
    initialize_database(path)
    clauses: list[str] = []
    parameters: list[Any] = []
    if session_id:
        clauses.append("session_id = ?")
        parameters.append(session_id)
    where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
    parameters.append(max(1, limit))
    with closing(connect(path)) as connection:
        rows = connection.execute(
            f"""
            SELECT *
            FROM turn_stop_snapshot
            {where}
            ORDER BY asof_at DESC, session_id, turn_id
            LIMIT ?
            """,
            parameters,
        ).fetchall()
    return [dict(row) for row in rows]


def latest_final(
    path: str | Path | None = None,
    *,
    session_id: str | None = None,
    turn_id: str | None = None,
) -> str | None:
    initialize_database(path)
    clauses = ["final_text IS NOT NULL"]
    parameters: list[Any] = []
    if session_id:
        clauses.append("session_id = ?")
        parameters.append(session_id)
    if turn_id:
        clauses.append("turn_id = ?")
        parameters.append(turn_id)
    with closing(connect(path)) as connection:
        row = connection.execute(
            f"""
            SELECT final_text
            FROM turn_stop_snapshot
            WHERE {' AND '.join(clauses)}
            ORDER BY asof_at DESC
            LIMIT 1
            """,
            parameters,
        ).fetchone()
    return None if row is None else row["final_text"]


def aggregate_latest(path: str | Path | None = None) -> dict[str, int]:
    """Aggregate only the newest cumulative snapshot from each session."""

    initialize_database(path)
    with closing(connect(path)) as connection:
        row = connection.execute(
            """
            WITH ranked AS (
                SELECT *,
                       ROW_NUMBER() OVER (
                           PARTITION BY session_id
                           ORDER BY asof_at DESC, turn_id DESC
                       ) AS snapshot_rank
                FROM turn_stop_snapshot
            )
            SELECT
                COUNT(*) AS sessions,
                COALESCE(SUM(input_tokens), 0) AS input_tokens,
                COALESCE(SUM(cached_input_tokens), 0) AS cached_input_tokens,
                COALESCE(SUM(cache_write_input_tokens), 0) AS cache_write_input_tokens,
                COALESCE(SUM(output_tokens), 0) AS output_tokens,
                COALESCE(SUM(reasoning_output_tokens), 0) AS reasoning_output_tokens,
                COALESCE(SUM(goal_tokens_used), 0) AS goal_visible_tokens
            FROM ranked
            WHERE snapshot_rank = 1
            """
        ).fetchone()

    result = {key: int(row[key]) for key in row.keys()}
    result["uncached_input_tokens"] = max(
        result["input_tokens"] - result["cached_input_tokens"], 0
    )
    result["total_tokens"] = result["input_tokens"] + result["output_tokens"]
    return result


def _path(path: str | Path | None) -> Path:
    return Path(path).expanduser() if path is not None else default_database_path()
