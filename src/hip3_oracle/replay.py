"""Deterministic replay of journaled feed decisions, without network or publishing."""

from __future__ import annotations

import json
from decimal import Decimal
from pathlib import Path
from typing import Any

from .aggregation import PriceAggregator
from .config import FeedConfig
from .models import MarketStatus, Quote
from .risk import CircuitBreaker, RiskDecision
from .state import StateError

DECIMAL_FIELDS = {
    "max_spread_bps",
    "outlier_mad_multiplier",
    "outlier_min_band_bps",
    "max_confidence_bps",
    "max_jump_bps",
    "max_group_weight_share_bps",
}


def replay_event(event: dict[str, Any]) -> dict[str, Any]:
    matches: dict[str, bool] = {}
    for coin, recorded in event["feeds"].items():
        raw_feed = dict(recorded["config"])
        raw_feed["sources"] = tuple(raw_feed["sources"])
        for field in DECIMAL_FIELDS:
            if field in raw_feed:
                raw_feed[field] = Decimal(str(raw_feed[field]))
        feed = FeedConfig(**raw_feed)
        quotes = [
            Quote(
                coin=item["coin"],
                source=item["source"],
                independence_group=item["independenceGroup"],
                price=Decimal(item["price"]),
                observed_at_ms=item["observedAtMs"],
                received_at_ms=item["receivedAtMs"],
                weight=Decimal(item["weight"]),
                market_status=MarketStatus(item["marketStatus"]),
                bid=Decimal(item["bid"]) if item["bid"] is not None else None,
                ask=Decimal(item["ask"]) if item["ask"] is not None else None,
            )
            for item in recorded["quotes"]
        ]
        aggregate = None
        error = None
        try:
            aggregate = PriceAggregator().aggregate(feed, quotes, now_ms=recorded["nowMs"])
            previous = Decimal(recorded["previousPrice"]) if recorded["previousPrice"] is not None else None
            decision = CircuitBreaker().evaluate(feed, aggregate, previous)
            if decision.decision is RiskDecision.BLOCK:
                error = f"circuit breaker: {decision.reason}"
        except Exception as exc:
            error = str(exc)
        matches[coin] = (
            error == recorded["error"] and (aggregate.as_dict() if aggregate else None) == recorded["aggregate"]
        )
    return {"matches": all(matches.values()), "feeds": matches, "timestampMs": event["timestampMs"]}


def replay_journal(path: Path) -> dict[str, Any]:
    cycles = 0
    mismatches: list[int] = []
    try:
        with path.open(encoding="utf-8") as handle:
            for line_number, line in enumerate(handle, 1):
                event = json.loads(line)
                if event.get("event") == "evaluated":
                    cycles += 1
                    if not replay_event(event)["matches"]:
                        mismatches.append(line_number)
    except (OSError, ValueError, KeyError, TypeError):
        raise StateError("invalid or unreadable audit journal") from None
    return {"matches": cycles > 0 and not mismatches, "cycles": cycles, "mismatchedLines": mismatches}
