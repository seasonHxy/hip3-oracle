from __future__ import annotations

import asyncio
import time
from dataclasses import asdict, dataclass
from decimal import Decimal
from typing import Any, Mapping

from .aggregation import PriceAggregator
from .audit import AuditLog
from .config import AppConfig, FeedConfig
from .locking import WriterLock
from .models import AggregatePrice, Quote
from .monitoring import RuntimeMonitor
from .publisher import Publisher, PublishError, PublishUncertainError, build_payload
from .readback import wire_coin
from .risk import CircuitBreaker, RiskDecision
from .sources import PriceSource
from .state import JsonStateStore, StateError


@dataclass(frozen=True, slots=True)
class FeedCycle:
    coin: str
    aggregate: AggregatePrice | None
    error: str | None
    source_errors: Mapping[str, str]
    quotes: tuple[Quote, ...] = ()
    evaluated_at_ms: int = 0
    previous_price: Decimal | None = None


@dataclass(frozen=True, slots=True)
class CycleResult:
    published: bool
    reason: str
    feeds: Mapping[str, FeedCycle]
    publisher_response: Any = None
    status: str = "blocked"
    mode: str = "dry-run"

    def as_dict(self) -> dict[str, Any]:
        return {
            "published": self.published,
            "reason": self.reason,
            "status": self.status,
            "mode": self.mode,
            "feeds": {
                coin: {
                    "aggregate": item.aggregate.as_dict() if item.aggregate else None,
                    "error": item.error,
                    "sourceErrors": dict(item.source_errors),
                }
                for coin, item in sorted(self.feeds.items())
            },
            "publisherResponse": self.publisher_response,
        }


class OracleService:
    def __init__(
        self,
        config: AppConfig,
        sources: Mapping[str, PriceSource],
        publisher: Publisher,
        state: JsonStateStore,
        *,
        aggregator: PriceAggregator | None = None,
        circuit_breaker: CircuitBreaker | None = None,
        monitor: RuntimeMonitor | None = None,
        audit: AuditLog | None = None,
    ):
        self.config = config
        self.sources = sources
        self.publisher = publisher
        self.state = state
        if state.scope is None:
            state.scope = config.state_scope
        elif state.scope != config.state_scope:
            raise StateError("service state scope differs from configuration")
        self.aggregator = aggregator or PriceAggregator()
        self.circuit_breaker = circuit_breaker or CircuitBreaker()
        self.monitor = monitor or RuntimeMonitor(config.monitoring, mode="dry-run" if config.dry_run else "live")
        self.audit = audit or AuditLog(state.path)
        self._cycle_lock = asyncio.Lock()
        self._storage_fault: str | None = None

    async def cycle(self) -> CycleResult:
        async with self._cycle_lock:
            started = time.monotonic()
            if self._storage_fault:
                result = CycleResult(False, self._storage_fault, {}, status="uncertain")
            else:
                try:
                    with WriterLock(self.state.path):
                        self.state.reload()
                        result = await self._cycle()
                except StateError as exc:
                    self._storage_fault = f"storage or writer lock failure: {exc}"
                    result = CycleResult(False, self._storage_fault, {}, status="uncertain")
            result = CycleResult(
                result.published,
                result.reason,
                result.feeds,
                result.publisher_response,
                result.status,
                "dry-run" if self.config.dry_run else "live",
            )
            self.monitor.record_cycle(result, time.monotonic() - started)
            return result

    async def _cycle(self) -> CycleResult:
        if self.state.pending is not None:
            return CycleResult(
                False, "unresolved publication; run reconcile before publishing again", {}, status="uncertain"
            )
        if not self.config.dry_run:
            missing = [feed.coin for feed in self.config.feeds if self.state.previous_price(feed.coin) is None]
            if missing:
                return CycleResult(False, "live state needs bootstrap from the DEX oracle before first publishing", {})
            remaining_ms = self.state.last_published_at_ms() + 2_500 - int(time.time() * 1000)
            if remaining_ms > 2_500:
                return CycleResult(False, "clock moved behind the persisted publication reference", {})
            if remaining_ms > 0:
                # Wait before fetching, so spacing cannot make the new observations stale.
                await asyncio.sleep(remaining_ms / 1000)
        outcomes = await asyncio.gather(*(self._process_feed(feed) for feed in self.config.feeds))
        feeds = {outcome.coin: outcome for outcome in outcomes}
        self.audit.append(
            {
                "event": "evaluated",
                "timestampMs": int(time.time() * 1000),
                "scope": self.config.state_scope,
                "feeds": {
                    feed.coin: {
                        "config": asdict(feed),
                        "nowMs": feeds[feed.coin].evaluated_at_ms,
                        "previousPrice": str(feeds[feed.coin].previous_price)
                        if feeds[feed.coin].previous_price is not None
                        else None,
                        "quotes": [quote.as_dict() for quote in feeds[feed.coin].quotes],
                        "aggregate": feeds[feed.coin].aggregate.as_dict() if feeds[feed.coin].aggregate else None,
                        "error": feeds[feed.coin].error,
                        "sourceErrors": dict(feeds[feed.coin].source_errors),
                    }
                    for feed in self.config.feeds
                },
            }
        )
        failures = [outcome for outcome in outcomes if outcome.error]
        if failures:
            reason = "; ".join(f"{item.coin}: {item.error}" for item in failures)
            return CycleResult(False, f"fail-closed batch: {reason}", feeds)

        aggregates = {item.coin: item.aggregate for item in outcomes if item.aggregate is not None}
        payload = build_payload(self.config, aggregates)
        values = {
            coin: (Decimal(payload.oracle_pxs[wire_coin(self.config.dex, coin)]), aggregate.observed_at_ms)
            for coin, aggregate in aggregates.items()
        }
        if not self.config.dry_run:
            self.state.prepare(payload.as_action(), values)
            self.audit.append(
                {
                    "event": "prepared",
                    "timestampMs": int(time.time() * 1000),
                    "pendingId": self.state.pending["id"],
                    "action": payload.as_action(),
                }
            )
        try:
            response = await asyncio.wait_for(
                self.publisher.publish(payload), timeout=self.config.publish_timeout_ms / 1000
            )
            if not self.config.dry_run and (
                not isinstance(response, dict) or response.get("confirmation", {}).get("verified") is not True
            ):
                raise PublishUncertainError("live publisher returned no verified readback")
        except (PublishUncertainError, TimeoutError):
            self.audit.append({"event": "uncertain", "timestampMs": int(time.time() * 1000)})
            return CycleResult(False, "publication outcome unknown; reconciliation required", feeds, status="uncertain")
        except PublishError as exc:
            if not self.config.dry_run:
                self.state.clear_pending()
            self.audit.append({"event": "publish_rejected", "timestampMs": int(time.time() * 1000), "reason": str(exc)})
            return CycleResult(False, f"publish failed: {exc}", feeds)
        except Exception:
            return CycleResult(
                False, "unexpected publisher failure; reconciliation required", feeds, status="uncertain"
            )

        published_at_ms = int(time.time() * 1000)
        self.audit.append(
            {
                "event": "simulated" if self.config.dry_run else "confirmed",
                "timestampMs": published_at_ms,
                "action": payload.as_action(),
            }
        )
        self.state.commit(values, published_at_ms)
        status = "simulated" if self.config.dry_run else "published"
        return CycleResult(True, status, feeds, response, status=status)

    async def _fetch(self, feed: FeedConfig, source_name: str) -> Quote:
        started = time.monotonic()
        failed = True
        try:
            result = await asyncio.wait_for(
                self.sources[source_name].get_quote(feed.coin), timeout=self.config.source_timeout_ms / 1000
            )
            failed = False
            return result
        finally:
            self.monitor.record_source(feed.coin, source_name, time.monotonic() - started, failed=failed)

    async def _process_feed(self, feed: FeedConfig) -> FeedCycle:
        tasks = [self._fetch(feed, source_name) for source_name in feed.sources]
        results = await asyncio.gather(*tasks, return_exceptions=True)
        quotes: list[Quote] = []
        source_errors: dict[str, str] = {}
        for source_name, result in zip(feed.sources, results, strict=True):
            if isinstance(result, asyncio.CancelledError):
                raise result
            if isinstance(result, BaseException):
                source_errors[source_name] = "source timeout" if isinstance(result, TimeoutError) else str(result)
            else:
                quotes.append(result)
        now_ms = int(time.time() * 1000)
        previous_price = self.state.previous_price(feed.coin)
        try:
            aggregate = self.aggregator.aggregate(feed, quotes, now_ms=now_ms)
            decision = self.circuit_breaker.evaluate(feed, aggregate, previous_price)
            if decision.decision is RiskDecision.BLOCK:
                return FeedCycle(
                    feed.coin,
                    aggregate,
                    f"circuit breaker: {decision.reason}",
                    source_errors,
                    tuple(quotes),
                    now_ms,
                    previous_price,
                )
            return FeedCycle(feed.coin, aggregate, None, source_errors, tuple(quotes), now_ms, previous_price)
        except Exception as exc:
            return FeedCycle(feed.coin, None, str(exc), source_errors, tuple(quotes), now_ms, previous_price)

    async def run_forever(self) -> None:
        while True:
            started = time.monotonic()
            result = await self.cycle()
            print(self._summary(result), flush=True)
            elapsed = time.monotonic() - started
            await asyncio.sleep(max(0, self.config.interval_ms / 1000 - elapsed))

    @staticmethod
    def _summary(result: CycleResult) -> str:
        import json

        return json.dumps({"timestampMs": int(time.time() * 1000), **result.as_dict()}, sort_keys=True, default=str)
