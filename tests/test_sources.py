import asyncio
import json
import threading
import unittest
import urllib.error
from decimal import Decimal
from unittest.mock import Mock, patch

from hip3_oracle.config import SourceConfig
from hip3_oracle.models import MarketStatus
from hip3_oracle.sources import JsonRestSource, SourceError, TransientSourceError


class FakeResponse:
    def __init__(self, payload: dict):
        self.payload = json.dumps(payload).encode()

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def read(self, _limit: int) -> bytes:
        return self.payload


class JsonSourceTests(unittest.IsolatedAsyncioTestCase):
    async def test_extracts_scales_and_maps_quote(self) -> None:
        config = SourceConfig(
            name="provider",
            kind="json",
            independence_group="provider-a",
            weight=Decimal("2"),
            options={
                "url": "https://provider.example/{symbol}",
                "symbols": {"AAPL": "AAPL.US"},
                "pricePath": "data.mid",
                "bidPath": "data.bid",
                "askPath": "data.ask",
                "priceScale": "0.01",
                "timestampPath": "data.ts",
                "timestampUnit": "ms",
                "statusPath": "data.session",
                "statusMap": {"REGULAR": "open"},
            },
        )
        response = FakeResponse(
            {"data": {"mid": "23020", "bid": "23019", "ask": "23021", "ts": 1_800_000_000_000, "session": "regular"}}
        )
        with patch("urllib.request.build_opener") as opener:
            opener.return_value.open.return_value = response
            result = await JsonRestSource(config).get_quote("AAPL")
        self.assertEqual(result.price, Decimal("230.20"))
        self.assertEqual(result.bid, Decimal("230.19"))
        self.assertEqual(result.ask, Decimal("230.21"))
        self.assertEqual(result.market_status, MarketStatus.OPEN)
        self.assertEqual(result.observed_at_ms, 1_800_000_000_000)

    def source(self, **overrides):
        return JsonRestSource(
            SourceConfig(
                "provider",
                "json",
                "provider",
                Decimal("1"),
                {
                    "url": "https://provider.example/quote",
                    "pricePath": "px",
                    "timestampPath": "ts",
                    "retryDelayMs": 0,
                    **overrides,
                },
            )
        )

    async def test_transient_http_error_is_retried_once(self):
        error = urllib.error.HTTPError("https://provider.example", 503, "down", {}, None)
        with patch("urllib.request.build_opener") as opener:
            opener.return_value.open.side_effect = [error, FakeResponse({"px": "100", "ts": 1000})]
            quote = await self.source().get_quote("AAPL")
            self.assertEqual(quote.price, Decimal("100"))
            self.assertEqual(opener.return_value.open.call_count, 2)

    async def test_auth_error_is_not_retried_or_leaked(self):
        error = urllib.error.HTTPError("https://provider.example/?token=secret", 401, "secret", {}, None)
        with patch("urllib.request.build_opener") as opener:
            opener.return_value.open.side_effect = error
            with self.assertRaisesRegex(SourceError, "HTTP 401") as caught:
                await self.source().get_quote("AAPL")
            self.assertNotIn("secret", str(caught.exception))
            self.assertEqual(opener.return_value.open.call_count, 1)

    async def test_bad_json_field_is_not_retried(self):
        with patch("urllib.request.build_opener") as opener:
            opener.return_value.open.return_value = FakeResponse({"wrong": "100"})
            with self.assertRaises(SourceError):
                await self.source().get_quote("AAPL")
            self.assertEqual(opener.return_value.open.call_count, 1)

    async def test_cancellation_does_not_spawn_another_http_worker(self):
        started = threading.Event()
        finish = threading.Event()
        source = self.source()

        def slow(_coin):
            started.set()
            finish.wait(timeout=2)
            return Mock()

        with patch.object(source, "_get_quote_sync", side_effect=slow) as worker:
            task = asyncio.create_task(source.get_quote("AAPL"))
            await asyncio.to_thread(started.wait, 1)
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task
            try:
                with self.assertRaisesRegex(TransientSourceError, "in flight"):
                    await source.get_quote("AAPL")
                self.assertEqual(worker.call_count, 1)
            finally:
                finish.set()
                await asyncio.shield(source._inflight)


if __name__ == "__main__":
    unittest.main()
