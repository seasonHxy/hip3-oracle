"""Local bounded JSONL journal containing normalized quotes, never credentials."""

from __future__ import annotations

import json
import logging
import os
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Any

from .state import StateError


class PrivateRotatingHandler(RotatingFileHandler):
    def _open(self):
        fd = os.open(self.baseFilename, os.O_CREAT | os.O_APPEND | os.O_WRONLY, 0o600)
        return os.fdopen(fd, "a", encoding="utf-8")


class AuditLog:
    def __init__(self, state_path: Path):
        self.path = state_path.with_suffix(".audit.jsonl")
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._handler = PrivateRotatingHandler(
            self.path, maxBytes=10_000_000, backupCount=5, encoding="utf-8", delay=True
        )
        self._handler.setFormatter(logging.Formatter("%(message)s"))

    def append(self, event: dict[str, Any]) -> None:
        try:
            encoded = json.dumps(event, sort_keys=True, separators=(",", ":"), default=str, allow_nan=False)
            record = logging.LogRecord("hip3_oracle.audit", logging.INFO, "", 0, encoded, (), None)
            if self._handler.shouldRollover(record):
                self._handler.doRollover()
            if self._handler.stream is None:
                fd = os.open(self.path, os.O_CREAT | os.O_APPEND | os.O_WRONLY, 0o600)
                self._handler.stream = os.fdopen(fd, "a", encoding="utf-8")
            self._handler.stream.write(encoded + "\n")
            self._handler.flush()
            os.fsync(self._handler.stream.fileno())
        except (OSError, ValueError):
            raise StateError("cannot persist audit journal") from None

    def close(self) -> None:
        self._handler.close()
