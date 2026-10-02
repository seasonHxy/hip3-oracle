import json
import unittest
import urllib.error
import urllib.request
from dataclasses import replace
from unittest.mock import patch

from hip3_oracle.config import MonitoringConfig
from hip3_oracle.monitoring import MonitoringServer, RuntimeMonitor
from hip3_oracle.service import CycleResult


class MonitoringTests(unittest.TestCase):
    def test_readiness_is_false_before_success_and_after_block_or_staleness(self):
        monitor = RuntimeMonitor(MonitoringConfig(), mode="dry-run")
        self.assertFalse(monitor.health()["ready"])
        with patch("hip3_oracle.monitoring.time.monotonic", return_value=10):
            monitor.record_cycle(CycleResult(True, "simulated", {}, status="simulated"), 0.1)
            self.assertTrue(monitor.health()["ready"])
        with patch("hip3_oracle.monitoring.time.monotonic", return_value=21):
            self.assertFalse(monitor.health()["ready"])
        monitor.record_cycle(CycleResult(False, "blocked", {}), 0.1)
        self.assertFalse(monitor.health()["ready"])

    def test_prometheus_escapes_labels_and_distinguishes_mode(self):
        monitor = RuntimeMonitor(MonitoringConfig(), mode="live")
        monitor.record_source("demo:AAPL", 'provider"\\\n', 0.25, failed=True)
        metrics = monitor.prometheus()
        self.assertIn("hip3_oracle_live_mode 1", metrics)
        self.assertIn('provider\\"\\\\\\n', metrics)
        self.assertIn("hip3_oracle_source_failures_total", metrics)

    def test_actual_http_endpoints_report_live_but_not_ready_before_first_cycle(self):
        monitor = RuntimeMonitor(replace(MonitoringConfig(), enabled=True, port=0), mode="dry-run")
        with MonitoringServer(monitor) as server:
            base = f"http://127.0.0.1:{server.address[1]}"
            with urllib.request.urlopen(base + "/livez", timeout=2) as response:
                self.assertTrue(json.load(response)["alive"])
            with self.assertRaises(urllib.error.HTTPError) as caught:
                urllib.request.urlopen(base + "/readyz", timeout=2)
            self.assertEqual(caught.exception.code, 503)
            with urllib.request.urlopen(base + "/metrics", timeout=2) as response:
                self.assertIn(b"hip3_oracle_ready 0", response.read())
            monitor.record_cycle(CycleResult(True, "simulated", {}, status="simulated"), 0.1)
            with urllib.request.urlopen(base + "/readyz", timeout=2) as response:
                self.assertTrue(json.load(response)["ready"])
