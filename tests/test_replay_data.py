import json
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from hip3_oracle.replay import replay_journal
from hip3_oracle.state import StateError

ROOT = Path(__file__).resolve().parents[1]


class ReplayDataTests(unittest.TestCase):
    def test_synthetic_cases_replay_exactly(self):
        path = ROOT / "examples" / "synthetic-replay.jsonl"
        result = replay_journal(path)
        self.assertTrue(result["matches"])
        self.assertEqual(result["cycles"], 6)
        records = [json.loads(line) for line in path.read_text().splitlines()]
        self.assertTrue(all(record["dataOrigin"] == "synthetic" for record in records))
        self.assertEqual(len({record["scenario"] for record in records}), 6)
        by_scenario = {record["scenario"]: record["feeds"]["demo:SYNTH"] for record in records}
        self.assertEqual(by_scenario["normal"]["aggregate"]["price"], "100.10")
        self.assertEqual(
            by_scenario["outlier"]["aggregate"]["rejected"], [{"source": "bad", "reason": "MAD outlier group"}]
        )
        self.assertIsNone(by_scenario["stale_quorum"]["aggregate"])
        self.assertIn("maxGroupWeightShareBps", by_scenario["weight_concentration"]["error"])
        self.assertEqual(by_scenario["market_closed"]["error"], "circuit breaker: market is closed")
        self.assertIn("10020.0000 bps", by_scenario["price_jump"]["error"])

    def test_empty_feed_event_is_not_accepted_as_successful_replay(self):
        with TemporaryDirectory() as directory:
            path = Path(directory) / "empty.jsonl"
            path.write_text('{"event":"evaluated","feeds":{},"timestampMs":1}\n')
            with self.assertRaises(StateError):
                replay_journal(path)

    def test_scalar_record_is_rejected_without_traceback(self):
        with TemporaryDirectory() as directory:
            path = Path(directory) / "bad.jsonl"
            path.write_text("[]\n")
            with self.assertRaises(StateError):
                replay_journal(path)
