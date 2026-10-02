"""Bounded, read-only Hyperliquid info queries; no wallet needed."""

from __future__ import annotations

import json
import urllib.error
import urllib.request
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from typing import Any, Mapping

from .config import AppConfig


class ReadbackError(RuntimeError):
    pass


def wire_coin(dex: str, coin: str) -> str:
    if ":" not in coin:
        return f"{dex}:{coin}"
    if not coin.startswith(f"{dex}:") or coin.count(":") != 1:
        raise ReadbackError("asset namespace does not match dex")
    return coin


@dataclass(frozen=True, slots=True)
class MarketSnapshot:
    decimals: Mapping[str, int]
    oracle_prices: Mapping[str, Decimal]

    def require_batch(self, expected: Mapping[str, str], decimals: Mapping[str, int]) -> None:
        if set(self.decimals) != set(expected):
            raise ReadbackError("configured batch does not match the complete DEX universe")
        if dict(self.decimals) != dict(decimals):
            raise ReadbackError("szDecimals differs from the DEX metadata")

    def require_prices(self, expected: Mapping[str, str]) -> None:
        for coin, value in expected.items():
            if self.oracle_prices.get(coin) != Decimal(value):
                raise ReadbackError(f"oraclePx readback mismatch for {coin}")


class HyperliquidReader:
    def __init__(self, config: AppConfig):
        self.config = config

    def read(self) -> MarketSnapshot:
        base_url = self.config.state_scope["apiUrl"]
        request = urllib.request.Request(
            base_url + "/info",
            data=json.dumps({"type": "metaAndAssetCtxs", "dex": self.config.dex}).encode(),
            headers={"Content-Type": "application/json", "User-Agent": "hip3-oracle/0.2"},
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=self.config.publish_timeout_ms / 1000) as response:
                body = response.read(2_000_001)
            if len(body) > 2_000_000:
                raise ReadbackError("info response exceeds size limit")
            raw = json.loads(body)
        except (urllib.error.URLError, TimeoutError, OSError, ValueError):
            raise ReadbackError("Hyperliquid info query failed") from None
        return parse_snapshot(raw, self.config.dex)


def parse_snapshot(raw: Any, dex: str) -> MarketSnapshot:
    try:
        if not isinstance(raw, list) or len(raw) != 2:
            raise ValueError
        universe = raw[0]["universe"]
        contexts = raw[1]
        if not isinstance(universe, list) or not isinstance(contexts, list) or not universe:
            raise ValueError
        decimals: dict[str, int] = {}
        prices: dict[str, Decimal] = {}
        for asset, context in zip(universe, contexts, strict=True):
            coin = wire_coin(dex, asset["name"])
            size = asset["szDecimals"]
            if coin in decimals or isinstance(size, bool) or not isinstance(size, int) or not 0 <= size <= 6:
                raise ValueError
            price = Decimal(str(context["oraclePx"]))
            if not price.is_finite() or price <= 0:
                raise ValueError
            decimals[coin] = size
            prices[coin] = price
        return MarketSnapshot(decimals, prices)
    except (KeyError, TypeError, ValueError, InvalidOperation):
        raise ReadbackError("invalid DEX metadata or oracle context") from None
