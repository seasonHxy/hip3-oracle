"""Explicit bootstrap and pending-publication reconciliation, using read-only API calls."""

from __future__ import annotations

import time
from decimal import Decimal
from typing import Any

from .audit import AuditLog
from .config import AppConfig
from .locking import WriterLock
from .readback import HyperliquidReader, wire_coin
from .state import JsonStateStore, StateError


def operate(
    config: AppConfig, operation: str, *, reason: str, reader: HyperliquidReader | None = None
) -> dict[str, Any]:
    if config.dry_run:
        raise StateError("bootstrap and reconcile require a live configuration")
    if len(reason.strip()) < 8:
        raise StateError("provide an operational reason of at least 8 characters")
    with WriterLock(config.state_file):
        state = JsonStateStore(config.state_file, scope=config.state_scope)
        snapshot = (reader or HyperliquidReader(config)).read()
        decimals = {wire_coin(config.dex, feed.coin): feed.sz_decimals for feed in config.feeds}
        now_ms = int(time.time() * 1000)
        if operation == "bootstrap":
            if state.pending is not None or any(state.previous_price(feed.coin) is not None for feed in config.feeds):
                raise StateError("bootstrap refuses to overwrite existing or pending state")
            snapshot.require_batch({coin: str(price) for coin, price in snapshot.oracle_prices.items()}, decimals)
            values = {
                feed.coin: (snapshot.oracle_prices[wire_coin(config.dex, feed.coin)], now_ms) for feed in config.feeds
            }
        elif operation == "reconcile":
            if state.pending is None:
                raise StateError("no pending publication to reconcile")
            pending = state.pending
            action = pending["action"].get("setOracle", {})
            if action.get("dex") != config.dex or set(pending["feeds"]) != {feed.coin for feed in config.feeds}:
                raise StateError("pending action does not match current configuration")
            expected = dict(action["oraclePxs"])
            snapshot.require_batch(expected, decimals)
            snapshot.require_prices(expected)
            values = {
                coin: (Decimal(value["last_price"]), int(value["last_observed_at_ms"]))
                for coin, value in pending["feeds"].items()
            }
            if any(expected[wire_coin(config.dex, coin)] != str(price) for coin, (price, _) in values.items()):
                raise StateError("pending state and action prices disagree")
        else:
            raise StateError("unknown operational command")
        audit = AuditLog(config.state_file)
        try:
            audit.append(
                {
                    "event": operation,
                    "timestampMs": now_ms,
                    "reason": reason,
                    "scope": config.state_scope,
                    "oraclePxs": {coin: str(price) for coin, (price, _) in values.items()},
                    "pendingId": state.pending["id"] if state.pending else None,
                }
            )
            state.commit(values, now_ms)
        finally:
            audit.close()
        return {"operation": operation, "stateFile": str(config.state_file), "success": True}
