"""Explicit local publisher controls, independent of signing keys and network access."""

from __future__ import annotations

import time
import uuid
from typing import Any

from .audit import AuditLog
from .config import AppConfig
from .locking import WriterLock
from .state import JsonStateStore, StateError


def status(config: AppConfig) -> dict[str, Any]:
    # Atomic replacement makes a file snapshot safe to read without creating a lock or directory.
    state = JsonStateStore(config.state_file, scope=config.state_scope)
    return state.overview(tuple(feed.coin for feed in config.feeds))


def control(config: AppConfig, operation: str, *, reason: str, pause_id: str | None = None) -> dict[str, Any]:
    if operation not in {"pause", "resume"}:
        raise StateError("unknown local control command")
    if len(reason.strip()) < 8:
        raise StateError("provide an operational reason of at least 8 characters")
    with WriterLock(config.state_file):
        state = JsonStateStore(config.state_file, scope=config.state_scope)
        if operation == "pause" and state.pause is not None:
            return {
                "operation": operation,
                "changed": False,
                **state.overview(tuple(feed.coin for feed in config.feeds)),
            }
        if operation == "resume" and (state.pause is None or pause_id != state.pause["id"]):
            raise StateError("resume requires the matching active pause ID")
        record = {"id": uuid.uuid4().hex, "pausedAtMs": int(time.time() * 1000), "reason": reason.strip()}
        target = record if operation == "pause" else None
        audit = AuditLog(config.state_file)
        try:
            audit.append(
                {
                    "event": "control_requested",
                    "operation": operation,
                    "scope": config.state_scope,
                    "timestampMs": int(time.time() * 1000),
                    "reason": reason.strip(),
                    "pauseId": record["id"] if target else pause_id,
                }
            )
            state.set_pause(target)
            audit.append(
                {
                    "event": "control_applied",
                    "operation": operation,
                    "scope": config.state_scope,
                    "timestampMs": int(time.time() * 1000),
                    "pauseId": record["id"] if target else pause_id,
                }
            )
        finally:
            audit.close()
        return {"operation": operation, "changed": True, **state.overview(tuple(feed.coin for feed in config.feeds))}
