import asyncio
import io
import json
import os
import select
import signal
import subprocess
import sys
import unittest
from contextlib import redirect_stdout
from dataclasses import replace
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import AsyncMock, patch

from hip3_oracle.config import load_config
from hip3_oracle.publisher import DryRunPublisher
from hip3_oracle.service import CycleResult, OracleService
from hip3_oracle.sources import build_sources
from hip3_oracle.state import JsonStateStore

ROOT = Path(__file__).resolve().parents[1]


class ShutdownTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.directory = TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        config = replace(load_config(ROOT / "config/example.json"), state_file=Path(self.directory.name) / "state.json")
        self.service = OracleService(
            config,
            build_sources(config.sources),
            DryRunPublisher(emit=False),
            JsonStateStore(config.state_file, scope=config.state_scope),
        )
        self.addCleanup(self.service.audit.close)

    async def test_stop_before_start_does_not_run_a_cycle(self):
        stop = asyncio.Event()
        stop.set()
        with patch.object(self.service, "cycle", new_callable=AsyncMock) as cycle:
            await self.service.run_forever(stop)
        cycle.assert_not_awaited()

    async def test_stop_during_cycle_finishes_it_without_starting_another(self):
        stop = asyncio.Event()
        entered = asyncio.Event()
        finish = asyncio.Event()

        async def active_cycle():
            entered.set()
            await finish.wait()
            return CycleResult(True, "simulated", {}, status="simulated")

        with patch.object(self.service, "cycle", side_effect=active_cycle) as cycle, redirect_stdout(io.StringIO()):
            task = asyncio.create_task(self.service.run_forever(stop))
            await asyncio.wait_for(entered.wait(), timeout=1)
            stop.set()
            await asyncio.sleep(0)
            self.assertFalse(task.done())
            finish.set()
            await asyncio.wait_for(task, timeout=1)
            self.assertEqual(cycle.await_count, 1)

    async def test_stop_interrupts_interval_sleep(self):
        stop = asyncio.Event()
        completed = asyncio.Event()

        async def cycle_once():
            completed.set()
            return CycleResult(True, "simulated", {}, status="simulated")

        with patch.object(self.service, "cycle", side_effect=cycle_once) as cycle, redirect_stdout(io.StringIO()):
            task = asyncio.create_task(self.service.run_forever(stop))
            await asyncio.wait_for(completed.wait(), timeout=1)
            await asyncio.sleep(0)
            stop.set()
            await asyncio.wait_for(task, timeout=0.5)
            self.assertEqual(cycle.await_count, 1)


class SignalIntegrationTests(unittest.TestCase):
    def test_sigterm_finishes_and_saves_a_real_cli_cycle(self):
        with TemporaryDirectory() as directory:
            raw = json.loads((ROOT / "config/example.json").read_text())
            raw["stateFile"] = "state.json"
            raw["monitoring"]["enabled"] = False
            path = Path(directory) / "config.json"
            path.write_text(json.dumps(raw))
            env = {**os.environ, "PYTHONPATH": str(ROOT / "src")}
            process = subprocess.Popen(
                [sys.executable, "-m", "hip3_oracle", "--config", str(path), "run"],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                env=env,
                cwd=ROOT,
            )
            try:
                ready, _, _ = select.select([process.stdout], [], [], 5)
                self.assertTrue(ready, "CLI did not produce its first cycle result")
                first = json.loads(process.stdout.readline())
                self.assertEqual(first["status"], "simulated")
                process.send_signal(signal.SIGTERM)
                _, errors = process.communicate(timeout=3)
                self.assertEqual(process.returncode, 0, errors)
                config = load_config(path)
                stored = JsonStateStore(config.state_file, scope=config.state_scope)
                self.assertIsNotNone(stored.previous_price("AAPL"))
                self.assertIsNone(stored.pending)
            finally:
                if process.poll() is None:
                    process.kill()
                    process.communicate(timeout=3)
