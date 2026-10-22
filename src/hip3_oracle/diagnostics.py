"""Read-only publication checks: collect and evaluate, but never sign or save state."""

from __future__ import annotations

import asyncio
import time
from typing import Any, Mapping

from .config import AppConfig, FeedConfig
from .monitoring import RuntimeMonitor
from .pipeline import fetch_quote, process_feed
from .publisher import PublishError, build_payload
from .readback import HyperliquidReader, ReadbackError, wire_coin
from .sources import PriceSource, build_sources
from .state import JsonStateStore, StateError


async def preflight(
    config: AppConfig,
    *,
    sources: Mapping[str, PriceSource] | None = None,
    reader: HyperliquidReader | None = None,
) -> dict[str, Any]:
    checks: list[dict[str, Any]] = []
    report: dict[str, Any] = {
        "operation": "preflight",
        "network": config.network,
        "dex": config.dex,
        "mode": "dry-run" if config.dry_run else "live",
        "checks": checks,
        "signed": False,
        "submitted": False,
        "stateWritten": False,
        "permissionsChecked": False,
    }
    try:
        state = JsonStateStore(config.state_file, scope=config.state_scope)
    except StateError as exc:
        checks.append({"check": "state", "passed": False, "detail": str(exc)})
        return {**report, "ok": False}
    checks.append({"check": "state", "passed": True})
    checks.append({"check": "operator_pause", "passed": state.pause is None})
    checks.append({"check": "pending_publication", "passed": state.pending is None})
    if not config.dry_run:
        checks.append(
            {"check": "bootstrap", "passed": all(state.previous_price(feed.coin) is not None for feed in config.feeds)}
        )
    adapters = sources if sources is not None else build_sources(config.sources)
    monitor = RuntimeMonitor(config.monitoring, mode=report["mode"])

    async def fetch(feed: FeedConfig, name: str):
        return await fetch_quote(config, feed, name, adapters, monitor)

    outcomes = await asyncio.gather(
        *(process_feed(feed, fetch, previous_price=state.previous_price(feed.coin)) for feed in config.feeds)
    )
    report["feeds"] = {
        outcome.coin: {
            "aggregate": outcome.aggregate.as_dict() if outcome.aggregate else None,
            "error": outcome.error,
            "sourceErrors": dict(outcome.source_errors),
        }
        for outcome in outcomes
    }
    for outcome in outcomes:
        checks.append({"check": f"feed:{outcome.coin}", "passed": outcome.error is None, "detail": outcome.error})
    aggregates = {item.coin: item.aggregate for item in outcomes if item.aggregate is not None and item.error is None}
    try:
        payload = build_payload(config, aggregates)
        report["candidateAction"] = payload.as_action()
        checks.append({"check": "complete_payload", "passed": True})
    except (PublishError, ValueError, ArithmeticError, ReadbackError) as exc:
        checks.append({"check": "complete_payload", "passed": False, "detail": str(exc)})
        return {**report, "ok": False}
    if not config.dry_run:
        try:
            snapshot = await asyncio.wait_for(
                asyncio.to_thread((reader or HyperliquidReader(config)).read),
                timeout=config.publish_timeout_ms / 1000,
            )
            snapshot.require_batch(
                payload.oracle_pxs, {wire_coin(config.dex, feed.coin): feed.sz_decimals for feed in config.feeds}
            )
            checks.append({"check": "dex_universe_and_precision", "passed": True})
            now_ms = int(time.time() * 1000)
            fresh = all(
                -feed.max_future_ms <= now_ms - aggregates[feed.coin].observed_at_ms <= feed.max_source_age_ms
                for feed in config.feeds
            )
            checks.append({"check": "fresh_after_preflight", "passed": fresh})
        except (ReadbackError, TimeoutError) as exc:
            checks.append(
                {
                    "check": "dex_universe_and_precision",
                    "passed": False,
                    "detail": str(exc) or "info query timed out",
                }
            )
    return {**report, "ok": all(check["passed"] for check in checks)}
