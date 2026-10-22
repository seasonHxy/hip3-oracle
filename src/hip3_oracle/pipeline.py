"""The same quote collection and decision path for publishing and read-only checks."""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass
from decimal import Decimal
from typing import Awaitable, Callable, Mapping

from .aggregation import PriceAggregator
from .config import AppConfig, FeedConfig
from .models import AggregatePrice, Quote
from .monitoring import RuntimeMonitor
from .risk import CircuitBreaker, RiskDecision
from .sources import PriceSource, SourceError


@dataclass(frozen=True, slots=True)
class FeedCycle:
    coin: str
    aggregate: AggregatePrice | None
    error: str | None
    source_errors: Mapping[str, str]
    quotes: tuple[Quote, ...] = ()
    evaluated_at_ms: int = 0
    previous_price: Decimal | None = None


async def fetch_quote(
    config: AppConfig,
    feed: FeedConfig,
    source_name: str,
    sources: Mapping[str, PriceSource],
    monitor: RuntimeMonitor,
) -> Quote:
    started = time.monotonic()
    failed = True
    try:
        result = await asyncio.wait_for(
            sources[source_name].get_quote(feed.coin), timeout=config.source_timeout_ms / 1000
        )
        expected = config.sources[source_name]
        if not isinstance(result, Quote) or (
            result.coin != feed.coin
            or result.source != source_name
            or result.independence_group != expected.independence_group
            or result.weight != expected.weight
        ):
            raise SourceError(f"source {source_name}: quote identity or trust configuration mismatch")
        failed = False
        return result
    finally:
        monitor.record_source(feed.coin, source_name, time.monotonic() - started, failed=failed)


def evaluate_quotes(
    feed: FeedConfig,
    quotes: list[Quote],
    *,
    now_ms: int,
    previous_price: Decimal | None,
    source_errors: Mapping[str, str] | None = None,
    aggregator: PriceAggregator | None = None,
    circuit_breaker: CircuitBreaker | None = None,
) -> FeedCycle:
    errors = source_errors or {}
    aggregate = None
    try:
        aggregate = (aggregator or PriceAggregator()).aggregate(feed, quotes, now_ms=now_ms)
        decision = (circuit_breaker or CircuitBreaker()).evaluate(feed, aggregate, previous_price)
        error = f"circuit breaker: {decision.reason}" if decision.decision is RiskDecision.BLOCK else None
        return FeedCycle(feed.coin, aggregate, error, errors, tuple(quotes), now_ms, previous_price)
    except Exception as exc:
        return FeedCycle(feed.coin, None, str(exc), errors, tuple(quotes), now_ms, previous_price)


async def process_feed(
    feed: FeedConfig,
    fetch: Callable[[FeedConfig, str], Awaitable[Quote]],
    *,
    previous_price: Decimal | None,
    aggregator: PriceAggregator | None = None,
    circuit_breaker: CircuitBreaker | None = None,
) -> FeedCycle:
    results = await asyncio.gather(*(fetch(feed, name) for name in feed.sources), return_exceptions=True)
    quotes: list[Quote] = []
    errors: dict[str, str] = {}
    for name, result in zip(feed.sources, results, strict=True):
        if isinstance(result, asyncio.CancelledError):
            raise result
        if isinstance(result, BaseException):
            errors[name] = "source timeout" if isinstance(result, TimeoutError) else str(result)
        else:
            quotes.append(result)
    return evaluate_quotes(
        feed,
        quotes,
        now_ms=int(time.time() * 1000),
        previous_price=previous_price,
        source_errors=errors,
        aggregator=aggregator,
        circuit_breaker=circuit_breaker,
    )
