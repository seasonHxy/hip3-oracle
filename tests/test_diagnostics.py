import json
import unittest
from dataclasses import replace
from decimal import Decimal
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import Mock, patch

from hip3_oracle.config import load_config
from hip3_oracle.control import control
from hip3_oracle.diagnostics import preflight
from hip3_oracle.readback import MarketSnapshot, ReadbackError
from hip3_oracle.sources import build_sources
from hip3_oracle.state import JsonStateStore

ROOT = Path(__file__).resolve().parents[1]


class DiagnosticTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.directory = TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.config = replace(
            load_config(ROOT / "config/example.json"), state_file=Path(self.directory.name) / "new" / "state.json"
        )
        self.reader = Mock()
        self.reader.read.return_value = MarketSnapshot({"demo:AAPL": 2}, {"demo:AAPL": Decimal("230.2")})

    def live(self):
        config = replace(self.config, dry_run=False)
        state = JsonStateStore(config.state_file, scope=config.state_scope)
        state.commit({"AAPL": (Decimal("230.2"), 1)}, 1)
        return config

    async def test_dry_preflight_uses_same_outlier_filter_without_writes(self):
        with patch("urllib.request.urlopen", side_effect=AssertionError("network forbidden")):
            report = await preflight(self.config)
        self.assertTrue(report["ok"])
        self.assertFalse(report["submitted"])
        self.assertFalse(report["signed"])
        self.assertFalse(report["stateWritten"])
        self.assertEqual(report["candidateAction"]["setOracle"]["oraclePxs"], [["demo:AAPL", "230.2"]])
        self.assertEqual(report["feeds"]["AAPL"]["aggregate"]["rejected"][0]["source"], "bad-outlier")
        self.assertFalse(self.config.state_file.parent.exists())

    async def test_live_preflight_without_private_key_only_reads(self):
        config = self.live()
        before = config.state_file.read_bytes()
        with (
            patch.dict("os.environ", {}, clear=True),
            patch("hip3_oracle.publisher.SdkPublisher", side_effect=AssertionError),
        ):
            report = await preflight(config, reader=self.reader)
        self.assertTrue(report["ok"])
        self.assertFalse(report["permissionsChecked"])
        self.assertEqual(before, config.state_file.read_bytes())
        self.assertEqual(list(config.state_file.parent.iterdir()), [config.state_file])

    async def test_preflight_detects_missing_bootstrap(self):
        report = await preflight(replace(self.config, dry_run=False), reader=self.reader)
        self.assertFalse(report["ok"])
        self.assertFalse(next(check for check in report["checks"] if check["check"] == "bootstrap")["passed"])
        self.assertFalse(self.config.state_file.parent.exists())

    async def test_preflight_reports_pause_without_removing_it(self):
        pause_id = control(self.config, "pause", reason="provider investigation")["pause"]["id"]
        report = await preflight(self.config)
        self.assertFalse(report["ok"])
        self.assertEqual(JsonStateStore(self.config.state_file, scope=self.config.state_scope).pause["id"], pause_id)

    async def test_live_universe_mismatch_fails_without_changing_state(self):
        config = self.live()
        before = config.state_file.read_bytes()
        self.reader.read.return_value = MarketSnapshot({"demo:AAPL": 3}, {"demo:AAPL": Decimal("230.2")})
        report = await preflight(config, reader=self.reader)
        self.assertFalse(report["ok"])
        self.assertIn("szDecimals", report["checks"][-1]["detail"])
        self.assertEqual(before, config.state_file.read_bytes())

    async def test_source_identity_cannot_claim_another_trust_group(self):
        sources = build_sources(self.config.sources)
        original = sources["venue-a"].get_quote

        async def forged(coin):
            return replace(await original(coin), independence_group="forged", weight=Decimal("9"))

        sources["venue-a"].get_quote = forged
        report = await preflight(self.config, sources=sources)
        self.assertFalse(report["ok"])
        self.assertIn("trust configuration mismatch", report["feeds"]["AAPL"]["sourceErrors"]["venue-a"])

    async def test_no_partial_candidate_is_returned_when_a_feed_fails(self):
        config = replace(self.config, feeds=(*self.config.feeds, replace(self.config.feeds[0], coin="TSLA")))
        report = await preflight(config)
        self.assertFalse(report["ok"])
        self.assertNotIn("candidateAction", report)

    async def test_info_failure_is_reported_without_submitting(self):
        self.reader.read.side_effect = ReadbackError("unavailable")
        report = await preflight(self.live(), reader=self.reader)
        self.assertFalse(report["ok"])
        self.assertEqual(report["checks"][-1]["detail"], "unavailable")

    async def test_scope_corruption_fails_before_data_requests(self):
        config = self.live()
        raw = json.loads(config.state_file.read_text())
        raw["scope"]["network"] = "mainnet"
        config.state_file.write_text(json.dumps(raw))
        report = await preflight(config, reader=self.reader)
        self.assertFalse(report["ok"])
        self.reader.read.assert_not_called()
