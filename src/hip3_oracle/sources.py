from __future__ import annotations

import asyncio
import json
import os
import re
import time
import urllib.error
import urllib.parse
import urllib.request
from decimal import Decimal, InvalidOperation
from typing import Any, Mapping, Protocol

from .config import SourceConfig
from .models import MarketStatus, Quote


class SourceError(RuntimeError):
    pass


class TransientSourceError(SourceError):
    """Only transport failures, 429 and 5xx are safe to retry."""


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *_args, **_kwargs):
        return None  # Never forward an Authorization header to a redirect target.


def _integer_option(options: Mapping[str, Any], name: str, default: int) -> int:
    value = options.get(name, default)
    if isinstance(value, bool) or not isinstance(value, (int, str)):
        raise SourceError(f"{name} must be an integer")
    try:
        return int(value)
    except ValueError:
        raise SourceError(f"{name} must be an integer") from None


class PriceSource(Protocol):
    name: str

    async def get_quote(self, coin: str) -> Quote: ...


def _decimal(value: Any, label: str) -> Decimal:
    try:
        result = Decimal(str(value))
    except (InvalidOperation, ValueError) as exc:
        raise SourceError(f"{label} is not a decimal") from exc
    if not result.is_finite():
        raise SourceError(f"{label} is not finite")
    return result


def _status(value: Any, mapping: Mapping[str, Any] | None = None) -> MarketStatus:
    text = str(value).strip().lower()
    if mapping:
        text = str(mapping.get(text, text)).strip().lower()
    try:
        return MarketStatus(text)
    except ValueError:
        return MarketStatus.UNKNOWN


def _extract(data: Any, path: str) -> Any:
    if not path:
        return data
    if path.startswith("/"):
        parts = [part.replace("~1", "/").replace("~0", "~") for part in path.split("/")[1:]]
    else:
        parts = path.split(".")
    value = data
    for part in parts:
        if isinstance(value, list):
            try:
                index = int(part)
                if index < 0:
                    raise IndexError
                value = value[index]
            except (ValueError, IndexError) as exc:
                raise SourceError(f"JSON path {path!r} does not exist") from exc
        elif isinstance(value, dict) and part in value:
            value = value[part]
        else:
            raise SourceError(f"JSON path {path!r} does not exist")
    return value


_ENV_PATTERN = re.compile(r"\$\{([A-Z_][A-Z0-9_]*)\}")


def _expand_env(value: str) -> str:
    def replace(match: re.Match[str]) -> str:
        name = match.group(1)
        if name not in os.environ:
            raise SourceError(f"required environment variable {name} is not set")
        return os.environ[name]

    return _ENV_PATTERN.sub(replace, value)


class StaticSource:
    def __init__(self, config: SourceConfig):
        self.name = config.name
        self.group = config.independence_group
        self.weight = config.weight
        self.prices = config.options.get("prices", {})
        if not isinstance(self.prices, dict):
            raise SourceError(f"source {self.name}: prices must be an object")
        self.market_status = _status(config.options.get("marketStatus", "open"))
        self.observed_at_offset_ms = _integer_option(config.options, "observedAtOffsetMs", 0)

    async def get_quote(self, coin: str) -> Quote:
        if coin not in self.prices:
            raise SourceError(f"source {self.name}: no static price for {coin}")
        now_ms = int(time.time() * 1000)
        return Quote(
            coin=coin,
            source=self.name,
            independence_group=self.group,
            price=_decimal(self.prices[coin], f"source {self.name} price"),
            observed_at_ms=now_ms + self.observed_at_offset_ms,
            received_at_ms=now_ms,
            weight=self.weight,
            market_status=self.market_status,
        )


class JsonRestSource:
    def __init__(self, config: SourceConfig):
        options = config.options
        self.name = config.name
        self.group = config.independence_group
        self.weight = config.weight
        self.url = str(options.get("url", ""))
        self.price_path = str(options.get("pricePath", ""))
        self.bid_path = str(options.get("bidPath", "")) or None
        self.ask_path = str(options.get("askPath", "")) or None
        self.timestamp_path = str(options.get("timestampPath", "")) or None
        self.timestamp_unit = str(options.get("timestampUnit", "ms")).lower()
        self.status_path = str(options.get("statusPath", "")) or None
        self.status_map = options.get("statusMap", {})
        self.symbols = options.get("symbols", {})
        self.timeout_seconds = _integer_option(options, "timeoutMs", 1_500) / 1000
        self.max_response_bytes = _integer_option(options, "maxResponseBytes", 1_000_000)
        self.price_scale = _decimal(options.get("priceScale", "1"), f"source {self.name} priceScale")
        self.headers = options.get("headers", {})
        self.max_retries = _integer_option(options, "maxRetries", 1)
        self.retry_delay_seconds = _integer_option(options, "retryDelayMs", 100) / 1000
        self.min_request_interval_seconds = _integer_option(options, "minRequestIntervalMs", 0) / 1000
        self._request_lock = asyncio.Lock()
        self._inflight: asyncio.Task[Quote] | None = None
        self._last_request_monotonic: float | None = None
        allow_insecure_http = options.get("allowInsecureHttp", False)
        if not isinstance(allow_insecure_http, bool):
            raise SourceError(f"source {self.name}: allowInsecureHttp must be a boolean")
        self.allow_insecure_http = allow_insecure_http
        if not self.url or not self.price_path:
            raise SourceError(f"source {self.name}: url and pricePath are required")
        if self.timestamp_unit not in {"ms", "s", "iso"}:
            raise SourceError(f"source {self.name}: timestampUnit must be ms, s, or iso")
        if self.timeout_seconds <= 0 or self.max_response_bytes <= 0:
            raise SourceError(f"source {self.name}: timeoutMs and maxResponseBytes must be positive")
        if self.price_scale <= 0:
            raise SourceError(f"source {self.name}: priceScale must be positive")
        if not 0 <= self.max_retries <= 2 or not 0 <= self.retry_delay_seconds <= 1:
            raise SourceError(f"source {self.name}: maxRetries must be 0-2 and retryDelayMs must be 0-1000")
        if self.min_request_interval_seconds < 0:
            raise SourceError(f"source {self.name}: minRequestIntervalMs must be nonnegative")
        if (
            not isinstance(self.symbols, dict)
            or not isinstance(self.headers, dict)
            or not isinstance(self.status_map, dict)
        ):
            raise SourceError(f"source {self.name}: symbols, headers, and statusMap must be objects")
        self.status_map = {str(key).lower(): value for key, value in self.status_map.items()}

    async def get_quote(self, coin: str) -> Quote:
        async with self._request_lock:
            if self._inflight is not None and not self._inflight.done():
                raise TransientSourceError(f"source {self.name}: previous request is still in flight")
            for attempt in range(self.max_retries + 1):
                if self._last_request_monotonic is not None:
                    delay = self.min_request_interval_seconds - (time.monotonic() - self._last_request_monotonic)
                    if delay > 0:
                        await asyncio.sleep(delay)
                self._last_request_monotonic = time.monotonic()
                try:
                    self._inflight = asyncio.create_task(asyncio.to_thread(self._get_quote_sync, coin))
                    self._inflight.add_done_callback(lambda task: task.exception() if not task.cancelled() else None)
                    return await asyncio.shield(self._inflight)
                except TransientSourceError:
                    if attempt == self.max_retries:
                        raise
                    await asyncio.sleep(self.retry_delay_seconds * (attempt + 1))
        raise SourceError(f"source {self.name}: no quote")  # Unreachable; keeps the return type explicit.

    def _get_quote_sync(self, coin: str) -> Quote:
        symbol = str(self.symbols.get(coin, coin))
        try:
            url = self.url.format(symbol=urllib.parse.quote(symbol, safe=""), coin=urllib.parse.quote(coin, safe=""))
        except (KeyError, ValueError) as exc:
            raise SourceError(f"source {self.name}: invalid URL template") from exc
        parsed = urllib.parse.urlparse(url)
        if parsed.username or parsed.password or not parsed.hostname:
            raise SourceError(f"source {self.name}: URL must not contain credentials and needs a host")
        if parsed.scheme != "https":
            local = parsed.hostname in {"127.0.0.1", "localhost", "::1"}
            if parsed.scheme != "http" or (not local and not self.allow_insecure_http):
                raise SourceError(f"source {self.name}: non-HTTPS URL refused")
        headers = {str(key): _expand_env(str(value)) for key, value in self.headers.items()}
        headers.setdefault("Accept", "application/json")
        headers.setdefault("User-Agent", "hip3-oracle/0.1")
        request = urllib.request.Request(url, headers=headers, method="GET")
        try:
            with urllib.request.build_opener(NoRedirect()).open(request, timeout=self.timeout_seconds) as response:
                raw = response.read(self.max_response_bytes + 1)
                if len(raw) > self.max_response_bytes:
                    raise SourceError(f"source {self.name}: response exceeds size limit")
        except urllib.error.HTTPError as exc:
            error_type = TransientSourceError if exc.code == 429 or 500 <= exc.code <= 599 else SourceError
            raise error_type(f"source {self.name}: HTTP {exc.code}") from None
        except (urllib.error.URLError, TimeoutError, OSError):
            raise TransientSourceError(f"source {self.name}: transport failure") from None
        try:
            data = json.loads(raw)
        except (json.JSONDecodeError, UnicodeDecodeError) as exc:
            raise SourceError(f"source {self.name}: invalid JSON response") from exc

        received_at_ms = int(time.time() * 1000)
        price = _decimal(_extract(data, self.price_path), f"source {self.name} price") * self.price_scale
        bid = (
            _decimal(_extract(data, self.bid_path), f"source {self.name} bid") * self.price_scale
            if self.bid_path
            else None
        )
        ask = (
            _decimal(_extract(data, self.ask_path), f"source {self.name} ask") * self.price_scale
            if self.ask_path
            else None
        )
        try:
            observed_at_ms = (
                self._parse_timestamp(_extract(data, self.timestamp_path)) if self.timestamp_path else received_at_ms
            )
        except (ValueError, TypeError, OverflowError, InvalidOperation):
            raise SourceError(f"source {self.name}: invalid timestamp") from None
        market_status = (
            _status(_extract(data, self.status_path), self.status_map) if self.status_path else MarketStatus.UNKNOWN
        )
        return Quote(
            coin=coin,
            source=self.name,
            independence_group=self.group,
            price=price,
            observed_at_ms=observed_at_ms,
            received_at_ms=received_at_ms,
            weight=self.weight,
            market_status=market_status,
            bid=bid,
            ask=ask,
        )

    def _parse_timestamp(self, value: Any) -> int:
        if self.timestamp_unit == "ms":
            return int(value)
        if self.timestamp_unit == "s":
            return int(Decimal(str(value)) * 1000)
        from datetime import datetime

        text = str(value).replace("Z", "+00:00")
        try:
            parsed = datetime.fromisoformat(text)
        except ValueError as exc:
            raise SourceError(f"source {self.name}: invalid ISO timestamp") from exc
        if parsed.tzinfo is None:
            raise SourceError(f"source {self.name}: ISO timestamp must include a timezone")
        return int(parsed.timestamp() * 1000)


def build_sources(configs: Mapping[str, SourceConfig]) -> dict[str, PriceSource]:
    result: dict[str, PriceSource] = {}
    for name, config in configs.items():
        if config.kind == "static":
            result[name] = StaticSource(config)
        elif config.kind == "json":
            result[name] = JsonRestSource(config)
        else:  # Defensive: config parsing should reject this first.
            raise SourceError(f"unsupported source kind: {config.kind}")
    return result
