import unittest
from dataclasses import replace
from decimal import Decimal

from hip3_oracle.aggregation import AggregationError, PriceAggregator
from hip3_oracle.config import FeedConfig
from hip3_oracle.models import MarketStatus, Quote

NOW = 1_800_000_000_000


def feed(**overrides) -> FeedConfig:
    values = {
        "coin": "AAPL",
        "sz_decimals": 2,
        "sources": ("a", "b", "c", "bad"),
        "min_sources": 3,
        "min_independent_groups": 3,
        "max_confidence_bps": Decimal("50"),
    }
    values.update(overrides)
    return FeedConfig(**values)


def quote(source: str, price: str, *, group: str | None = None, age_ms: int = 0) -> Quote:
    return Quote(
        coin="AAPL",
        source=source,
        independence_group=group or source,
        price=Decimal(price),
        observed_at_ms=NOW - age_ms,
        received_at_ms=NOW,
        market_status=MarketStatus.OPEN,
    )


class AggregationTests(unittest.TestCase):
    def test_unknown_source_status_is_not_overruled_by_open_sources(self) -> None:
        quotes = [
            quote("a", "100"),
            quote("b", "100.1"),
            replace(quote("c", "100.2"), market_status=MarketStatus.UNKNOWN),
        ]
        result = PriceAggregator().aggregate(feed(), quotes, now_ms=NOW)
        self.assertIs(result.market_status, MarketStatus.UNKNOWN)

    def test_dominant_group_is_blocked_before_mad(self) -> None:
        quotes = [replace(quote("a", "500"), weight=Decimal("9")), quote("b", "100"), quote("c", "100.1")]
        with self.assertRaisesRegex(AggregationError, "maxGroupWeightShareBps.*before"):
            PriceAggregator().aggregate(feed(), quotes, now_ms=NOW)

    def test_outlier_removal_can_make_a_group_too_dominant(self) -> None:
        quotes = [
            replace(quote("a", "100"), weight=Decimal("2")),
            quote("b", "100.1"),
            quote("c", "100.2"),
            quote("bad", "500"),
        ]
        with self.assertRaisesRegex(AggregationError, "maxGroupWeightShareBps.*after"):
            PriceAggregator().aggregate(feed(), quotes, now_ms=NOW)

    def test_source_loss_cannot_bypass_weight_cap(self) -> None:
        quotes = [replace(quote("a", "100"), weight=Decimal("2")), quote("b", "100.1"), quote("c", "100.2")]
        with self.assertRaisesRegex(AggregationError, "maxGroupWeightShareBps"):
            PriceAggregator().aggregate(feed(), quotes, now_ms=NOW)

    def test_filters_outlier_and_uses_weighted_median(self) -> None:
        result = PriceAggregator().aggregate(
            feed(),
            [quote("a", "100.00"), quote("b", "100.10"), quote("c", "100.20"), quote("bad", "500")],
            now_ms=NOW,
        )
        self.assertEqual(result.price, Decimal("100.10"))
        self.assertEqual(result.source_count, 3)
        self.assertEqual(result.rejected[0].source, "bad")

    def test_stale_source_can_break_quorum(self) -> None:
        with self.assertRaises(AggregationError):
            PriceAggregator().aggregate(
                feed(),
                [quote("a", "100"), quote("b", "100.1"), quote("c", "100.2", age_ms=6_000)],
                now_ms=NOW,
            )

    def test_independence_groups_are_enforced(self) -> None:
        with self.assertRaisesRegex(AggregationError, "independent groups"):
            PriceAggregator().aggregate(
                feed(sources=("a", "b", "c")),
                [quote("a", "100", group="same"), quote("b", "100.1", group="same"), quote("c", "100.2")],
                now_ms=NOW,
            )

    def test_future_quote_is_rejected(self) -> None:
        future = quote("c", "100.2")
        future = Quote(
            coin=future.coin,
            source=future.source,
            independence_group=future.independence_group,
            price=future.price,
            observed_at_ms=NOW + 2_000,
            received_at_ms=NOW,
        )
        with self.assertRaises(AggregationError):
            PriceAggregator().aggregate(feed(), [quote("a", "100"), quote("b", "100.1"), future], now_ms=NOW)

    def test_same_upstream_group_cannot_accumulate_weight(self) -> None:
        config = feed(
            sources=("a", "a-mirror", "a-cache", "b", "c"),
            min_sources=5,
            min_independent_groups=3,
            outlier_min_band_bps=Decimal("200"),
            max_confidence_bps=Decimal("100"),
        )
        result = PriceAggregator().aggregate(
            config,
            [
                quote("a", "101", group="venue-a"),
                quote("a-mirror", "101", group="venue-a"),
                quote("a-cache", "101", group="venue-a"),
                quote("b", "100", group="venue-b"),
                quote("c", "100.1", group="venue-c"),
            ],
            now_ms=NOW,
        )
        self.assertEqual(result.price, Decimal("100.1"))
        self.assertEqual(result.source_count, 5)
        self.assertEqual(result.independent_group_count, 3)


if __name__ == "__main__":
    unittest.main()
