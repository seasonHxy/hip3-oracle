"""Deterministic replay of journaled feed decisions, without network or publishing."""

from __future__ import annotations

import json
from decimal import Decimal
from pathlib import Path
from typing import Any

from .config import FeedConfig
from .models import MarketStatus, Quote
from .pipeline import evaluate_quotes
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
    if not isinstance(event.get("feeds"), dict) or not event["feeds"]:
        raise StateError("evaluated event must contain at least one feed")
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
        previous = Decimal(recorded["previousPrice"]) if recorded["previousPrice"] is not None else None
        result = evaluate_quotes(feed, quotes, now_ms=recorded["nowMs"], previous_price=previous)
        matches[coin] = (
            result.error == recorded["error"]
            and (result.aggregate.as_dict() if result.aggregate else None) == recorded["aggregate"]
        )
    return {"matches": all(matches.values()), "feeds": matches, "timestampMs": event["timestampMs"]}


def replay_journal(path: Path) -> dict[str, Any]:
    cycles = 0
    mismatches: list[int] = []
    try:
        with path.open(encoding="utf-8") as handle:
            for line_number, line in enumerate(handle, 1):
                event = json.loads(line)
                if not isinstance(event, dict):
                    raise StateError("audit journal records must be objects")
                if event.get("event") == "evaluated":
                    cycles += 1
                    if not replay_event(event)["matches"]:
                        mismatches.append(line_number)
    except (OSError, ValueError, KeyError, TypeError):
        raise StateError("invalid or unreadable audit journal") from None
    return {"matches": cycles > 0 and not mismatches, "cycles": cycles, "mismatchedLines": mismatches}
