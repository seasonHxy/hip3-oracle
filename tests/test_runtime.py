import asyncio
import json
import time
import unittest
from dataclasses import replace
from decimal import Decimal
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import AsyncMock, Mock, patch

from hip3_oracle.config import load_config
from hip3_oracle.locking import WriterLock
from hip3_oracle.operations import operate
from hip3_oracle.publisher import DryRunPublisher, PublishError, PublishUncertainError
from hip3_oracle.readback import MarketSnapshot, ReadbackError, parse_snapshot
from hip3_oracle.replay import replay_journal
from hip3_oracle.service import OracleService
from hip3_oracle.sources import build_sources
from hip3_oracle.state import JsonStateStore, StateError

ROOT = Path(__file__).resolve().parents[1]


class RuntimeTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.directory = TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.config = replace(
            load_config(ROOT / "config/example.json"), state_file=Path(self.directory.name) / "state.json"
        )

    def service(self, *, live=False, publisher=None, seed=True, **overrides):
        config = replace(self.config, dry_run=not live, **overrides)
        state = JsonStateStore(config.state_file, scope=config.state_scope)
        if live and seed and not state.path.exists():
            state.commit({"AAPL": (Decimal("230.2"), 1)}, int(time.time() * 1000) - 10_000)
        service = OracleService(config, build_sources(config.sources), publisher or DryRunPublisher(emit=False), state)
        self.addCleanup(service.audit.close)
        return service

    async def test_dry_run_saves_formatted_price_and_replays_decision(self):
        service = self.service()
        result = await service.cycle()
        self.assertEqual(result.status, "simulated")
        self.assertEqual(service.state.previous_price("AAPL"), Decimal("230.2"))
        replay = replay_journal(service.audit.path)
        self.assertTrue(replay["matches"])
        self.assertEqual(replay["cycles"], 1)

    async def test_corrupted_replay_detects_changed_price(self):
        service = self.service()
        await service.cycle()
        event = json.loads(service.audit.path.read_text().splitlines()[0])
        event["feeds"]["AAPL"]["aggregate"]["price"] = "999"
        from hip3_oracle.replay import replay_event

        self.assertFalse(replay_event(event)["matches"])

    async def test_one_bad_feed_blocks_whole_batch(self):
        second = replace(self.config.feeds[0], coin="TSLA")
        publisher = DryRunPublisher(emit=False)
        service = self.service(publisher=publisher, feeds=(*self.config.feeds, second))
        result = await service.cycle()
        self.assertFalse(result.published)
        self.assertIn("fail-closed batch", result.reason)
        self.assertIsNone(publisher.last_payload)
        self.assertIsNone(service.state.previous_price("AAPL"))

    async def test_hanging_source_has_total_deadline_and_records_failure(self):
        service = self.service(source_timeout_ms=10)

        async def hang(_coin):
            await asyncio.sleep(10)

        service.sources["bad-outlier"] = Mock(get_quote=hang)
        result = await service.cycle()
        self.assertTrue(result.published)
        self.assertEqual(result.feeds["AAPL"].source_errors["bad-outlier"], "source timeout")
        self.assertEqual(service.monitor.source_failures[("AAPL", "bad-outlier")], 1)

    async def test_uncertain_outcome_survives_restart_and_prevents_resend(self):
        publisher = Mock(publish=AsyncMock(side_effect=PublishUncertainError("unknown")))
        service = self.service(live=True, publisher=publisher)
        first = await service.cycle()
        self.assertEqual(first.status, "uncertain")
        self.assertIsNotNone(service.state.pending)
        self.assertEqual(service.state.previous_price("AAPL"), Decimal("230.2"))
        restarted = self.service(live=True, publisher=publisher)
        second = await restarted.cycle()
        self.assertEqual(second.status, "uncertain")
        self.assertEqual(publisher.publish.await_count, 1)

    async def test_timeout_retains_pending_publication(self):
        async def hang(_payload):
            await asyncio.sleep(10)

        service = self.service(live=True, publisher=Mock(publish=hang), publish_timeout_ms=10)
        result = await service.cycle()
        self.assertEqual(result.status, "uncertain")
        self.assertIsNotNone(JsonStateStore(self.config.state_file, scope=service.config.state_scope).pending)

    async def test_definitive_rejection_clears_pending_but_retains_attempt_time(self):
        service = self.service(live=True, publisher=Mock(publish=AsyncMock(side_effect=PublishError("rejected"))))
        result = await service.cycle()
        self.assertFalse(result.published)
        self.assertIsNone(service.state.pending)
        self.assertGreater(service.state.last_attempt_at_ms, 0)
        service.state.last_attempt_at_ms = int(time.time() * 1000) - 2_480
        service.state.save()
        confirmed = Mock(publish=AsyncMock(return_value={"confirmation": {"verified": True}}))
        restarted = self.service(live=True, publisher=confirmed)
        started = time.monotonic()
        result = await restarted.cycle()
        self.assertTrue(result.published)
        self.assertGreater(time.monotonic() - started, 0.005)
        self.assertEqual(confirmed.publish.await_count, 1)

    async def test_no_bootstrap_prevents_live_submission(self):
        publisher = Mock(publish=AsyncMock())
        result = await self.service(live=True, publisher=publisher, seed=False).cycle()
        self.assertIn("bootstrap", result.reason)
        publisher.publish.assert_not_awaited()

    async def test_live_success_requires_readback_confirmation(self):
        service = self.service(live=True, publisher=Mock(publish=AsyncMock(return_value={"status": "ok"})))
        result = await service.cycle()
        self.assertEqual(result.status, "uncertain")
        self.assertIsNotNone(service.state.pending)

    async def test_confirmed_success_clears_pending_and_commits(self):
        service = self.service(
            live=True,
            publisher=Mock(publish=AsyncMock(return_value={"status": "ok", "confirmation": {"verified": True}})),
        )
        result = await service.cycle()
        self.assertTrue(result.published)
        self.assertIsNone(service.state.pending)
        self.assertEqual(result.mode, "live")

    async def test_audit_write_failure_blocks_before_publish(self):
        publisher = Mock(publish=AsyncMock())
        service = self.service(live=True, publisher=publisher)
        with patch.object(service.audit, "append", side_effect=StateError("disk full")):
            result = await service.cycle()
        self.assertFalse(result.published)
        publisher.publish.assert_not_awaited()

    async def test_state_commit_failure_retains_pending_and_blocks_next_cycle(self):
        publisher = Mock(publish=AsyncMock(return_value={"confirmation": {"verified": True}}))
        service = self.service(live=True, publisher=publisher)
        original_save = service.state.save

        def save_once():
            if service.state.pending is None:
                raise StateError("disk full during commit")
            original_save()

        with patch.object(service.state, "save", side_effect=save_once):
            result = await service.cycle()
        self.assertEqual(result.status, "uncertain")
        self.assertIsNotNone(service.state.pending)
        await service.cycle()
        self.assertEqual(publisher.publish.await_count, 1)

    async def test_concurrent_local_writers_cannot_send(self):
        publisher = Mock(publish=AsyncMock())
        service = self.service(publisher=publisher)
        with WriterLock(service.state.path):
            result = await service.cycle()
        self.assertFalse(result.published)
        publisher.publish.assert_not_awaited()


class ReadbackTests(unittest.TestCase):
    def test_parser_pairs_universe_with_context_by_position(self):
        snapshot = parse_snapshot(
            [
                {"universe": [{"name": "demo:AAPL", "szDecimals": 2}, {"name": "TSLA", "szDecimals": 3}]},
                [{"oraclePx": "230.2"}, {"oraclePx": "390.22"}],
            ],
            "demo",
        )
        self.assertEqual(snapshot.oracle_prices["demo:TSLA"], Decimal("390.22"))
        self.assertEqual(snapshot.decimals["demo:TSLA"], 3)

    def test_parser_rejects_missing_context_and_nonfinite_price(self):
        for contexts in ([], [{"oraclePx": "NaN"}]):
            with self.assertRaises(ReadbackError):
                parse_snapshot([{"universe": [{"name": "AAPL", "szDecimals": 2}]}, contexts], "demo")


class OperationsTests(unittest.TestCase):
    def setUp(self):
        self.directory = TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.config = replace(
            load_config(ROOT / "config/example.json"), dry_run=False, state_file=Path(self.directory.name) / "live.json"
        )
        self.reader = Mock()
        self.reader.read.return_value = MarketSnapshot({"demo:AAPL": 2}, {"demo:AAPL": Decimal("230.2")})

    def test_bootstrap_reads_reference_but_does_not_overwrite_it(self):
        result = operate(self.config, "bootstrap", reason="initial Testnet setup", reader=self.reader)
        self.assertTrue(result["success"])
        self.assertEqual(
            JsonStateStore(self.config.state_file, scope=self.config.state_scope).previous_price("AAPL"),
            Decimal("230.2"),
        )
        with self.assertRaisesRegex(StateError, "overwrite"):
            operate(self.config, "bootstrap", reason="initial Testnet setup", reader=self.reader)

    def test_reconciliation_only_commits_matching_complete_batch(self):
        state = JsonStateStore(self.config.state_file, scope=self.config.state_scope)
        state.prepare(
            {"type": "perpDeploy", "setOracle": {"dex": "demo", "oraclePxs": [["demo:AAPL", "230.2"]]}},
            {"AAPL": (Decimal("230.2"), 1)},
        )
        self.reader.read.return_value = MarketSnapshot({"demo:AAPL": 2}, {"demo:AAPL": Decimal("229")})
        with self.assertRaises(ReadbackError):
            operate(self.config, "reconcile", reason="operator checking timeout", reader=self.reader)
        self.assertIsNotNone(JsonStateStore(self.config.state_file, scope=self.config.state_scope).pending)
        self.reader.read.return_value = MarketSnapshot({"demo:AAPL": 2}, {"demo:AAPL": Decimal("230.2")})
        operate(self.config, "reconcile", reason="operator checking timeout", reader=self.reader)
        self.assertIsNone(JsonStateStore(self.config.state_file, scope=self.config.state_scope).pending)
