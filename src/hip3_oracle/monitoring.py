"""Small dependency-free health and Prometheus endpoints."""

from __future__ import annotations

import json
import threading
import time
from collections import Counter
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

from .config import MonitoringConfig


def _label(value: str) -> str:
    return value.replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n")


class RuntimeMonitor:
    def __init__(self, config: MonitoringConfig, *, mode: str):
        self.config = config
        self.mode = mode
        self._lock = threading.Lock()
        self.cycles: Counter[str] = Counter()
        self.source_failures: Counter[tuple[str, str]] = Counter()
        self.source_latency: dict[tuple[str, str], float] = {}
        self.feeds: dict[str, dict[str, Any]] = {}
        self.last_status = "starting"
        self.last_success_ms: int | None = None
        self.last_success_monotonic: float | None = None
        self.duration_seconds = 0.0

    def record_source(self, coin: str, source: str, seconds: float, *, failed: bool) -> None:
        with self._lock:
            key = (coin, source)
            self.source_latency[key] = seconds
            if failed:
                self.source_failures[key] += 1

    def record_cycle(self, result: Any, seconds: float) -> None:
        with self._lock:
            self.last_status = result.status
            self.cycles[result.status] += 1
            self.duration_seconds = seconds
            if result.published:
                self.last_success_ms = int(time.time() * 1000)
                self.last_success_monotonic = time.monotonic()
            self.feeds = {
                coin: {
                    "sources": item.aggregate.source_count if item.aggregate else 0,
                    "groups": item.aggregate.independent_group_count if item.aggregate else 0,
                    "confidenceBps": float(item.aggregate.confidence_bps) if item.aggregate else 0,
                    "blocked": bool(item.error),
                }
                for coin, item in result.feeds.items()
            }

    def health(self) -> dict[str, Any]:
        with self._lock:
            age = (
                (time.monotonic() - self.last_success_monotonic) * 1000
                if self.last_success_monotonic is not None
                else None
            )
            return {
                "alive": True,
                "ready": self.last_status in {"published", "simulated"}
                and age is not None
                and age <= self.config.readiness_max_age_ms,
                "mode": self.mode,
                "lastStatus": self.last_status,
                "lastSuccessAtMs": self.last_success_ms,
            }

    def prometheus(self) -> str:
        health = self.health()
        with self._lock:
            lines = [
                "# TYPE hip3_oracle_ready gauge",
                f"hip3_oracle_ready {int(health['ready'])}",
                "# TYPE hip3_oracle_live_mode gauge",
                f"hip3_oracle_live_mode {int(self.mode == 'live')}",
                "# TYPE hip3_oracle_cycles_total counter",
            ]
            for status, count in sorted(self.cycles.items()):
                lines.append(f'hip3_oracle_cycles_total{{status="{_label(status)}"}} {count}')
            lines.extend(
                [
                    "# TYPE hip3_oracle_cycle_duration_seconds gauge",
                    f"hip3_oracle_cycle_duration_seconds {self.duration_seconds}",
                    "# TYPE hip3_oracle_last_success_timestamp_seconds gauge",
                    f"hip3_oracle_last_success_timestamp_seconds {(self.last_success_ms or 0) / 1000}",
                    "# TYPE hip3_oracle_source_failures_total counter",
                ]
            )
            for (coin, source), count in sorted(self.source_failures.items()):
                labels = f'coin="{_label(coin)}",source="{_label(source)}"'
                lines.append(f"hip3_oracle_source_failures_total{{{labels}}} {count}")
            lines.append("# TYPE hip3_oracle_source_duration_seconds gauge")
            for (coin, source), seconds in sorted(self.source_latency.items()):
                labels = f'coin="{_label(coin)}",source="{_label(source)}"'
                lines.append(f"hip3_oracle_source_duration_seconds{{{labels}}} {seconds}")
            for metric, field in [("sources", "sources"), ("groups", "groups"), ("confidence_bps", "confidenceBps")]:
                lines.append(f"# TYPE hip3_oracle_{metric} gauge")
                for coin, values in sorted(self.feeds.items()):
                    lines.append(f'hip3_oracle_{metric}{{coin="{_label(coin)}"}} {values[field]}')
            return "\n".join(lines) + "\n"


class MonitoringServer:
    def __init__(self, monitor: RuntimeMonitor):
        self.monitor = monitor
        self._server: ThreadingHTTPServer | None = None
        self._thread: threading.Thread | None = None

    def __enter__(self) -> "MonitoringServer":
        if not self.monitor.config.enabled:
            return self
        monitor = self.monitor

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self) -> None:
                health = monitor.health()
                if self.path == "/metrics":
                    body = monitor.prometheus().encode()
                    content_type = "text/plain; version=0.0.4"
                    status = 200
                elif self.path in {"/livez", "/readyz", "/healthz"}:
                    body = json.dumps(health).encode()
                    content_type = "application/json"
                    status = 503 if self.path != "/livez" and not health["ready"] else 200
                else:
                    body, content_type, status = b"not found", "text/plain", 404
                self.send_response(status)
                self.send_header("Content-Type", content_type)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *_args: Any) -> None:
                pass

        self._server = ThreadingHTTPServer((monitor.config.host, monitor.config.port), Handler)
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)
        self._thread.start()
        return self

    @property
    def address(self) -> tuple[str, int] | None:
        return self._server.server_address if self._server else None

    def __exit__(self, *_args: object) -> None:
        if self._server:
            self._server.shutdown()
            self._server.server_close()
        if self._thread:
            self._thread.join(timeout=2)
