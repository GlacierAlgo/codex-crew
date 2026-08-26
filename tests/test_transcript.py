from __future__ import annotations

import json
from pathlib import Path
import tempfile
import unittest

from codex_crew.transcript import read_transcript_snapshot, shorten_goal_objective


class ObjectiveTests(unittest.TestCase):
    def test_keeps_objective_through_forty_characters(self) -> None:
        value = "甲" * 40
        self.assertEqual(value, shorten_goal_objective(value))

    def test_shortens_by_unicode_character(self) -> None:
        value = "甲" * 20 + "中" + "乙" * 20
        self.assertEqual("甲" * 20 + "..." + "乙" * 20, shorten_goal_objective(value))


class TranscriptTests(unittest.TestCase):
    def test_extracts_latest_usage_and_goal(self) -> None:
        objective = "前" * 20 + "需要省略的中间" + "后" * 20
        events = [
            {"type": "event_msg", "payload": {"type": "token_count", "info": {
                "total_token_usage": {"input_tokens": 10, "cached_input_tokens": 4,
                "cache_write_input_tokens": 1, "output_tokens": 2,
                "reasoning_output_tokens": 1}}}},
            {"type": "event_msg", "payload": {"type": "thread_goal_updated", "goal": {
                "objective": objective, "status": "active", "tokenBudget": 100,
                "tokensUsed": 12, "timeUsedSeconds": 8, "createdAt": 7}}},
            {"type": "event_msg", "payload": {"type": "token_count", "info": {
                "total_token_usage": {"input_tokens": 20, "cached_input_tokens": 8,
                "cache_write_input_tokens": 2, "output_tokens": 5,
                "reasoning_output_tokens": 3}}}},
        ]
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "rollout.jsonl"
            with path.open("w", encoding="utf-8") as stream:
                stream.write("not-json\n")
                for event in events:
                    stream.write(json.dumps(event, ensure_ascii=False) + "\n")
            snapshot = read_transcript_snapshot(path)

        self.assertIsNotNone(snapshot.usage)
        self.assertEqual(20, snapshot.usage.input_tokens)
        self.assertEqual(8, snapshot.usage.cached_input_tokens)
        self.assertIsNotNone(snapshot.goal)
        self.assertEqual("active", snapshot.goal.status)
        self.assertEqual("前" * 20 + "..." + "后" * 20, snapshot.goal.objective_excerpt)

    def test_goal_clear_removes_previous_goal(self) -> None:
        events = [
            {"type": "event_msg", "payload": {"type": "thread_goal_updated", "goal": {
                "objective": "temporary", "status": "active"}}},
            {"method": "thread/goal/cleared", "params": {"threadId": "session"}},
        ]
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "rollout.jsonl"
            path.write_text("\n".join(json.dumps(event) for event in events), encoding="utf-8")
            snapshot = read_transcript_snapshot(path)
        self.assertIsNone(snapshot.goal)


if __name__ == "__main__":
    unittest.main()
