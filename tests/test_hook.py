from __future__ import annotations

from io import StringIO
import json
from pathlib import Path
import tempfile
import unittest

from codex_crew.hook import capture_stop, run_stop_hook
from codex_crew.storage import latest_snapshots


class HookTests(unittest.TestCase):
    def test_capture_stop_persists_final_and_asof_usage(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            transcript = root / "rollout.jsonl"
            transcript.write_text(
                json.dumps(
                    {
                        "type": "event_msg",
                        "payload": {
                            "type": "token_count",
                            "info": {
                                "total_token_usage": {
                                    "input_tokens": 30,
                                    "cached_input_tokens": 20,
                                    "output_tokens": 6,
                                    "reasoning_output_tokens": 2,
                                }
                            },
                        },
                    }
                ),
                encoding="utf-8",
            )
            database = root / "snapshots.sqlite3"
            capture_stop(
                {
                    "session_id": "session",
                    "turn_id": "turn",
                    "model": "test-model",
                    "transcript_path": str(transcript),
                    "last_assistant_message": "finished",
                },
                database_path=database,
                asof_at=123,
            )
            row = latest_snapshots(database, limit=1)[0]

        self.assertEqual("finished", row["final_text"])
        self.assertEqual(30, row["input_tokens"])
        self.assertEqual(20, row["cached_input_tokens"])

    def test_hook_is_fail_open_and_has_no_tmux_projection(self) -> None:
        stdout = StringIO()
        stderr = StringIO()
        exit_code = run_stop_hook(
            stdin=StringIO("not-json"), stdout=stdout, stderr=stderr
        )
        self.assertEqual(0, exit_code)
        self.assertEqual("{}\n", stdout.getvalue())
        self.assertIn("failed to capture", stderr.getvalue())


if __name__ == "__main__":
    unittest.main()
