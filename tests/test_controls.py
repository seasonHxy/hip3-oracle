import json
import unittest
from dataclasses import replace
from decimal import Decimal
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import AsyncMock, Mock, patch

from hip3_oracle.config import load_config
from hip3_oracle.control import control, status
from hip3_oracle.publisher import DryRunPublisher
from hip3_oracle.service import OracleService
from hip3_oracle.sources import build_sources
from hip3_oracle.state import JsonStateStore, StateError

ROOT = Path(__file__).resolve().parents[1]


class ControlTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.directory = TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.config = replace(
            load_config(ROOT / "config/example.json"), state_file=Path(self.directory.name) / "new" / "state.json"
        )

    def test_status_is_a_snapshot_and_does_not_create_missing_directory(self):
        with patch("urllib.request.urlopen", side_effect=AssertionError("network forbidden")):
            report = status(self.config)
        self.assertFalse(report["exists"])
        self.assertEqual(report["processHealth"], "not_observed")
        self.assertFalse(self.config.state_file.parent.exists())

    def test_pause_is_persistent_idempotent_and_requires_matching_resume_id(self):
        first = control(self.config, "pause", reason="provider investigation")
        pause_id = first["pause"]["id"]
        self.assertEqual(status(self.config)["pause"]["id"], pause_id)
        repeated = control(self.config, "pause", reason="repeated operator request")
        self.assertFalse(repeated["changed"])
        self.assertEqual(repeated["pause"]["id"], pause_id)
        with self.assertRaisesRegex(StateError, "matching"):
            control(self.config, "resume", reason="incident resolved", pause_id="wrong")
        self.assertEqual(status(self.config)["pause"]["id"], pause_id)
        resumed = control(self.config, "resume", reason="incident resolved", pause_id=pause_id)
        self.assertIsNone(resumed["pause"])
        events = [
            json.loads(line)["event"]
            for line in self.config.state_file.with_suffix(".audit.jsonl").read_text().splitlines()
        ]
        self.assertEqual(events, ["control_requested", "control_applied", "control_requested", "control_applied"])

    def test_stale_resume_cannot_clear_new_incident(self):
        first = control(self.config, "pause", reason="first investigation")["pause"]["id"]
        control(self.config, "resume", reason="first incident resolved", pause_id=first)
        second = control(self.config, "pause", reason="second investigation")["pause"]["id"]
        self.assertNotEqual(first, second)
        with self.assertRaises(StateError):
            control(self.config, "resume", reason="stale console command", pause_id=first)
        self.assertEqual(status(self.config)["pause"]["id"], second)

    async def test_paused_cycle_never_fetches_or_publishes(self):
        control(self.config, "pause", reason="provider investigation")
        sources = {name: Mock(get_quote=AsyncMock()) for name in self.config.sources}
        publisher = Mock(publish=AsyncMock())
        service = OracleService(
            self.config, sources, publisher, JsonStateStore(self.config.state_file, scope=self.config.state_scope)
        )
        self.addCleanup(service.audit.close)
        result = await service.cycle()
        self.assertEqual(result.status, "paused")
        for source in sources.values():
            source.get_quote.assert_not_awaited()
        publisher.publish.assert_not_awaited()
        self.assertFalse(service.monitor.health()["ready"])

    async def test_resume_does_not_clear_pending_or_change_reference(self):
        self.config.state_file.parent.mkdir()
        state = JsonStateStore(self.config.state_file, scope=self.config.state_scope)
        state.commit({"AAPL": (Decimal("230.2"), 1)}, 1)
        state.prepare(
            {"type": "perpDeploy", "setOracle": {"dex": "demo", "oraclePxs": [["demo:AAPL", "230.2"]]}},
            {"AAPL": (Decimal("230.2"), 1)},
        )
        pending_id = state.pending["id"]
        pause_id = control(self.config, "pause", reason="unknown publication")["pause"]["id"]
        control(self.config, "resume", reason="resume after inspection", pause_id=pause_id)
        report = status(self.config)
        self.assertEqual(report["pending"]["id"], pending_id)
        self.assertEqual(report["references"]["AAPL"]["last_price"], "230.2")
        service = OracleService(
            self.config,
            build_sources(self.config.sources),
            DryRunPublisher(emit=False),
            JsonStateStore(self.config.state_file, scope=self.config.state_scope),
        )
        self.addCleanup(service.audit.close)
        self.assertEqual((await service.cycle()).status, "uncertain")

    def test_pause_requires_reason_and_does_not_change_state_if_audit_fails(self):
        with self.assertRaises(StateError):
            control(self.config, "pause", reason="short")
        with patch("hip3_oracle.control.AuditLog.append", side_effect=StateError("disk full")):
            with self.assertRaises(StateError):
                control(self.config, "pause", reason="provider investigation")
        self.assertFalse(self.config.state_file.exists())

    def test_schema_three_protects_pause_from_old_schema_two_writers(self):
        control(self.config, "pause", reason="provider investigation")
        raw = json.loads(self.config.state_file.read_text())
        self.assertEqual(raw["version"], 3)
        self.assertIsNotNone(raw["pause"])

    def test_invalid_pause_cannot_silently_enable_publishing(self):
        control(self.config, "pause", reason="provider investigation")
        raw = json.loads(self.config.state_file.read_text())
        raw["pause"]["reason"] = None
        self.config.state_file.write_text(json.dumps(raw))
        with self.assertRaisesRegex(StateError, "pause"):
            status(self.config)
