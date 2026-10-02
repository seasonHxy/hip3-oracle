import time
import unittest
from decimal import Decimal
from pathlib import Path
from unittest.mock import Mock

from hip3_oracle.config import AppConfig, FeedConfig
from hip3_oracle.models import AggregatePrice, MarketStatus
from hip3_oracle.publisher import PublishError, PublishUncertainError, SdkPublisher, build_payload
from hip3_oracle.readback import MarketSnapshot, ReadbackError


def aggregate(coin: str, price: str) -> AggregatePrice:
    return AggregatePrice(
        coin=coin,
        price=Decimal(price),
        confidence=Decimal("0.01"),
        confidence_bps=Decimal("1"),
        observed_at_ms=int(time.time() * 1000),
        source_count=3,
        independent_group_count=3,
        market_status=MarketStatus.OPEN,
        sources=("a", "b", "c"),
    )


class PayloadTests(unittest.TestCase):
    def config(self) -> AppConfig:
        return AppConfig(
            dex="demo",
            network="testnet",
            interval_ms=3000,
            dry_run=True,
            state_file=Path("state.json"),
            feeds=(
                FeedConfig("TSLA", 2, ("a", "b", "c")),
                FeedConfig("AAPL", 2, ("a", "b", "c")),
            ),
            sources={},
        )

    def test_action_tuples_are_lexicographically_sorted(self) -> None:
        payload = build_payload(
            self.config(),
            {"TSLA": aggregate("TSLA", "390.22"), "AAPL": aggregate("AAPL", "230.2")},
        )
        action = payload.as_action()["setOracle"]
        self.assertEqual([item[0] for item in action["oraclePxs"]], ["demo:AAPL", "demo:TSLA"])
        self.assertEqual(action["markPxs"], [])
        self.assertEqual(action["externalPerpPxs"], action["oraclePxs"])

    def test_partial_batch_is_refused(self) -> None:
        with self.assertRaises(PublishError):
            build_payload(self.config(), {"AAPL": aggregate("AAPL", "230.2")})


class FakeExchange:
    def __init__(self, response):
        self.response = response
        self.call = None

    def perp_deploy_set_oracle(self, dex, oracle_pxs, mark_pxs, external_perp_pxs):
        self.call = (dex, oracle_pxs, mark_pxs, external_perp_pxs)
        return self.response


class LiveAdapterTests(unittest.IsolatedAsyncioTestCase):
    def publisher(self, response=None):
        config = PayloadTests().config()
        payload = build_payload(config, {"TSLA": aggregate("TSLA", "390.22"), "AAPL": aggregate("AAPL", "230.2")})
        publisher = SdkPublisher(config, private_key="not-used-by-fake")
        publisher._exchange = FakeExchange(response or {"status": "ok"})
        publisher.reader = Mock()
        publisher.reader.read.return_value = MarketSnapshot(
            {"demo:AAPL": 2, "demo:TSLA": 2}, {"demo:AAPL": Decimal("230.2"), "demo:TSLA": Decimal("390.22")}
        )
        return publisher, payload

    async def test_calls_official_sdk_shape(self) -> None:
        config = PayloadTests().config()
        payload = build_payload(
            config,
            {"TSLA": aggregate("TSLA", "390.22"), "AAPL": aggregate("AAPL", "230.2")},
        )
        exchange = FakeExchange({"status": "ok", "response": {"type": "default"}})
        publisher = SdkPublisher(config, private_key="not-used-by-fake")
        publisher._exchange = exchange
        publisher.reader = self.publisher()[0].reader
        result = await publisher.publish(payload)
        self.assertEqual(result["status"], "ok")
        self.assertEqual(exchange.call[0], "demo")
        self.assertEqual(exchange.call[1], {"demo:AAPL": "230.2", "demo:TSLA": "390.22"})
        self.assertTrue(result["confirmation"]["verified"])

    async def test_rejected_sdk_response_raises(self) -> None:
        config = PayloadTests().config()
        payload = build_payload(
            config,
            {"TSLA": aggregate("TSLA", "390.22"), "AAPL": aggregate("AAPL", "230.2")},
        )
        publisher = SdkPublisher(config, private_key="not-used-by-fake")
        publisher._exchange = FakeExchange({"status": "err", "response": "rejected"})
        publisher.reader = self.publisher()[0].reader
        with self.assertRaises(PublishError):
            await publisher.publish(payload)

    async def test_missing_dex_asset_prevents_submission(self) -> None:
        publisher, payload = self.publisher()
        publisher.reader.read.return_value = MarketSnapshot({"demo:AAPL": 2}, {"demo:AAPL": Decimal("230.2")})
        with self.assertRaisesRegex(PublishError, "complete DEX universe"):
            await publisher.publish(payload)
        self.assertIsNone(publisher._exchange.call)

    async def test_wrong_size_decimals_prevents_submission(self) -> None:
        publisher, payload = self.publisher()
        publisher.reader.read.return_value = MarketSnapshot(
            {"demo:AAPL": 3, "demo:TSLA": 2}, {"demo:AAPL": Decimal("230.2"), "demo:TSLA": Decimal("390.22")}
        )
        with self.assertRaisesRegex(PublishError, "szDecimals"):
            await publisher.publish(payload)
        self.assertIsNone(publisher._exchange.call)

    async def test_accepted_but_readback_mismatch_is_uncertain(self) -> None:
        publisher, payload = self.publisher()
        publisher.reader.read.return_value = MarketSnapshot(
            {"demo:AAPL": 2, "demo:TSLA": 2}, {"demo:AAPL": Decimal("229"), "demo:TSLA": Decimal("390.22")}
        )
        with self.assertRaises(PublishUncertainError):
            await publisher.publish(payload)
        self.assertIsNotNone(publisher._exchange.call)
        self.assertEqual(publisher.reader.read.call_count, 4)

    async def test_exchange_transport_failure_is_not_retried(self) -> None:
        publisher, payload = self.publisher()
        publisher._exchange = Mock()
        publisher._exchange.perp_deploy_set_oracle.side_effect = TimeoutError("credentials should not appear")
        with self.assertRaisesRegex(PublishUncertainError, "outcome is unknown"):
            await publisher.publish(payload)
        self.assertEqual(publisher._exchange.perp_deploy_set_oracle.call_count, 1)

    async def test_confirmation_can_wait_for_eventual_readback(self) -> None:
        publisher, payload = self.publisher()
        valid = publisher.reader.read.return_value
        publisher.reader.read.side_effect = [valid, ReadbackError("not yet"), valid]
        result = await publisher.publish(payload)
        self.assertTrue(result["confirmation"]["verified"])

    async def test_second_submission_is_locally_rate_limited(self) -> None:
        publisher, payload = self.publisher()
        await publisher.publish(payload)
        with self.assertRaisesRegex(PublishError, "local rate limit"):
            await publisher.publish(payload)

    async def test_quote_expiring_during_preflight_is_not_submitted(self) -> None:
        from dataclasses import replace

        publisher, payload = self.publisher()
        payload = replace(payload, observed_at_ms={coin: 1 for coin in payload.oracle_pxs})
        with self.assertRaisesRegex(PublishError, "expired before submission"):
            await publisher.publish(payload)
        self.assertIsNone(publisher._exchange.call)


if __name__ == "__main__":
    unittest.main()
