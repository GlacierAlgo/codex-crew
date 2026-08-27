from __future__ import annotations

from contextlib import closing
from pathlib import Path
import sqlite3
import tempfile
import unittest

from codex_crew.storage import (
    StopSnapshot,
    aggregate_latest,
    latest_final,
    upsert_snapshot,
)


class StorageTests(unittest.TestCase):
    def test_upsert_is_idempotent_by_session_and_turn(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            database = Path(directory) / "snapshots.sqlite3"
            first = StopSnapshot(
                "session", "turn", 10, final_text="first", input_tokens=10
            )
            second = StopSnapshot(
                "session", "turn", 11, final_text="second", input_tokens=12
            )
            upsert_snapshot(first, database)
            upsert_snapshot(second, database)

            with closing(sqlite3.connect(database)) as connection:
                count = connection.execute(
                    "SELECT COUNT(*) FROM turn_stop_snapshot"
                ).fetchone()[0]
                application_tables = {
                    row[0]
                    for row in connection.execute(
                        "SELECT name FROM sqlite_master "
                        "WHERE type = 'table' AND name NOT LIKE 'sqlite_%'"
                    ).fetchall()
                }
            self.assertEqual(1, count)
            self.assertEqual({"turn_stop_snapshot"}, application_tables)
            self.assertEqual("second", latest_final(database, session_id="session"))

    def test_summary_uses_only_latest_snapshot_per_session(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            database = Path(directory) / "snapshots.sqlite3"
            upsert_snapshot(
                StopSnapshot(
                    "one",
                    "old",
                    10,
                    input_tokens=10,
                    cached_input_tokens=4,
                    output_tokens=2,
                    reasoning_output_tokens=1,
                    goal_tokens_used=9,
                ),
                database,
            )
            upsert_snapshot(
                StopSnapshot(
                    "one",
                    "new",
                    20,
                    input_tokens=20,
                    cached_input_tokens=8,
                    output_tokens=5,
                    reasoning_output_tokens=3,
                    goal_tokens_used=15,
                ),
                database,
            )
            upsert_snapshot(
                StopSnapshot(
                    "two",
                    "only",
                    15,
                    input_tokens=7,
                    cached_input_tokens=2,
                    output_tokens=3,
                    reasoning_output_tokens=1,
                    goal_tokens_used=4,
                ),
                database,
            )
            summary = aggregate_latest(database)

        self.assertEqual(2, summary["sessions"])
        self.assertEqual(27, summary["input_tokens"])
        self.assertEqual(10, summary["cached_input_tokens"])
        self.assertEqual(8, summary["output_tokens"])
        self.assertEqual(35, summary["total_tokens"])
        self.assertEqual(19, summary["goal_visible_tokens"])


if __name__ == "__main__":
    unittest.main()
