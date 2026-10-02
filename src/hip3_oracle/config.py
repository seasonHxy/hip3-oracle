from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any, Mapping


class ConfigError(ValueError):
    pass


def _decimal(value: Any, field_name: str) -> Decimal:
    try:
        result = Decimal(str(value))
    except (InvalidOperation, ValueError) as exc:
        raise ConfigError(f"{field_name} must be a decimal number") from exc
    if not result.is_finite():
        raise ConfigError(f"{field_name} must be finite")
    return result


def _boolean(value: Any, field_name: str) -> bool:
    if not isinstance(value, bool):
        raise ConfigError(f"{field_name} must be a boolean")
    return value


def _integer(value: Any, field_name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, (int, str)):
        raise ConfigError(f"{field_name} must be an integer")
    try:
        return int(value)
    except (TypeError, ValueError) as exc:
        raise ConfigError(f"{field_name} must be an integer") from exc


@dataclass(frozen=True, slots=True)
class SourceConfig:
    name: str
    kind: str
    independence_group: str
    weight: Decimal
    options: Mapping[str, Any] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class FeedConfig:
    coin: str
    sz_decimals: int
    sources: tuple[str, ...]
    min_sources: int = 3
    min_independent_groups: int = 3
    max_source_age_ms: int = 5_000
    max_future_ms: int = 1_000
    max_spread_bps: Decimal = Decimal("100")
    outlier_mad_multiplier: Decimal = Decimal("6")
    outlier_min_band_bps: Decimal = Decimal("10")
    max_confidence_bps: Decimal = Decimal("100")
    max_jump_bps: Decimal = Decimal("1_000")
    block_when_market_closed: bool = False
    block_when_market_unknown: bool = False
    max_group_weight_share_bps: Decimal = Decimal("4000")


@dataclass(frozen=True, slots=True)
class MonitoringConfig:
    enabled: bool = False
    host: str = "127.0.0.1"
    port: int = 9108
    readiness_max_age_ms: int = 10_000


@dataclass(frozen=True, slots=True)
class AppConfig:
    dex: str
    network: str
    interval_ms: int
    dry_run: bool
    state_file: Path
    feeds: tuple[FeedConfig, ...]
    sources: Mapping[str, SourceConfig]
    api_url: str | None = None
    mark_price_sets: int = 0
    source_timeout_ms: int = 2_000
    publish_timeout_ms: int = 5_000
    confirmation_attempts: int = 3
    confirmation_interval_ms: int = 200
    monitoring: MonitoringConfig = field(default_factory=MonitoringConfig)

    @property
    def state_scope(self) -> dict[str, str]:
        return {
            "network": self.network,
            "mode": "dry-run" if self.dry_run else "live",
            "dex": self.dex,
            "apiUrl": self.api_url or f"https://api.hyperliquid{'-testnet' if self.network == 'testnet' else ''}.xyz",
        }


def _require_dict(value: Any, field_name: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ConfigError(f"{field_name} must be an object")
    return value


def _parse_source(name: str, raw: Any) -> SourceConfig:
    if not name.strip():
        raise ConfigError("source names must not be empty")
    data = _require_dict(raw, f"sources.{name}")
    kind = str(data.get("kind", "")).strip().lower()
    if kind not in {"static", "json"}:
        raise ConfigError(f"sources.{name}.kind must be 'static' or 'json'")
    group = str(data.get("independenceGroup", "")).strip()
    if not group:
        raise ConfigError(f"sources.{name}.independenceGroup is required")
    weight = _decimal(data.get("weight", "1"), f"sources.{name}.weight")
    if weight <= 0:
        raise ConfigError(f"sources.{name}.weight must be positive")
    options = {key: value for key, value in data.items() if key not in {"kind", "independenceGroup", "weight"}}
    return SourceConfig(name=name, kind=kind, independence_group=group, weight=weight, options=options)


def _parse_feed(index: int, raw: Any, sources: Mapping[str, SourceConfig]) -> FeedConfig:
    data = _require_dict(raw, f"feeds[{index}]")
    coin = str(data.get("coin", "")).strip()
    if not coin:
        raise ConfigError(f"feeds[{index}].coin is required")
    sz_decimals = _integer(data.get("szDecimals", -1), f"feeds[{index}].szDecimals")
    if not 0 <= sz_decimals <= 6:
        raise ConfigError(f"feeds[{index}].szDecimals must be between 0 and 6")
    source_names_raw = data.get("sources")
    if not isinstance(source_names_raw, list) or not source_names_raw:
        raise ConfigError(f"feeds[{index}].sources must be a non-empty array")
    source_names = tuple(str(value) for value in source_names_raw)
    if len(source_names) != len(set(source_names)):
        raise ConfigError(f"feeds[{index}].sources contains duplicates")
    unknown = sorted(set(source_names) - set(sources))
    if unknown:
        raise ConfigError(f"feeds[{index}] references unknown sources: {', '.join(unknown)}")

    feed = FeedConfig(
        coin=coin,
        sz_decimals=sz_decimals,
        sources=source_names,
        min_sources=_integer(data.get("minSources", 3), f"feeds[{index}].minSources"),
        min_independent_groups=_integer(data.get("minIndependentGroups", 3), f"feeds[{index}].minIndependentGroups"),
        max_source_age_ms=_integer(data.get("maxSourceAgeMs", 5_000), f"feeds[{index}].maxSourceAgeMs"),
        max_future_ms=_integer(data.get("maxFutureMs", 1_000), f"feeds[{index}].maxFutureMs"),
        max_spread_bps=_decimal(data.get("maxSpreadBps", "100"), f"feeds[{index}].maxSpreadBps"),
        outlier_mad_multiplier=_decimal(data.get("outlierMadMultiplier", "6"), f"feeds[{index}].outlierMadMultiplier"),
        outlier_min_band_bps=_decimal(data.get("outlierMinBandBps", "10"), f"feeds[{index}].outlierMinBandBps"),
        max_confidence_bps=_decimal(data.get("maxConfidenceBps", "100"), f"feeds[{index}].maxConfidenceBps"),
        max_jump_bps=_decimal(data.get("maxJumpBps", "1000"), f"feeds[{index}].maxJumpBps"),
        block_when_market_closed=_boolean(
            data.get("blockWhenMarketClosed", False), f"feeds[{index}].blockWhenMarketClosed"
        ),
        block_when_market_unknown=_boolean(
            data.get("blockWhenMarketUnknown", False), f"feeds[{index}].blockWhenMarketUnknown"
        ),
        max_group_weight_share_bps=_decimal(
            data.get("maxGroupWeightShareBps", "4000"), f"feeds[{index}].maxGroupWeightShareBps"
        ),
    )
    if feed.min_sources < 1 or feed.min_sources > len(source_names):
        raise ConfigError(f"feeds[{index}].minSources must be between 1 and its source count")
    group_count = len({sources[name].independence_group for name in source_names})
    if feed.min_independent_groups < 1 or feed.min_independent_groups > group_count:
        raise ConfigError(f"feeds[{index}].minIndependentGroups exceeds its independent source groups")
    if not 0 < feed.max_group_weight_share_bps < 5_000:
        raise ConfigError(f"feeds[{index}].maxGroupWeightShareBps must be between 0 and 5000 exclusive")
    group_weights: dict[str, Decimal] = {}
    for name in source_names:
        source = sources[name]
        group_weights[source.independence_group] = max(
            group_weights.get(source.independence_group, Decimal(0)), source.weight
        )
    total_weight = sum(group_weights.values(), Decimal(0))
    if any(weight * 10_000 > total_weight * feed.max_group_weight_share_bps for weight in group_weights.values()):
        raise ConfigError(f"feeds[{index}] exceeds maxGroupWeightShareBps; add independent groups or reduce weight")
    if feed.max_source_age_ms <= 0 or feed.max_future_ms < 0:
        raise ConfigError(f"feeds[{index}] timestamp limits are invalid")
    for field_name, value in {
        "maxSpreadBps": feed.max_spread_bps,
        "outlierMadMultiplier": feed.outlier_mad_multiplier,
        "outlierMinBandBps": feed.outlier_min_band_bps,
        "maxConfidenceBps": feed.max_confidence_bps,
        "maxJumpBps": feed.max_jump_bps,
    }.items():
        if value < 0:
            raise ConfigError(f"feeds[{index}].{field_name} must not be negative")
    return feed


def parse_config(raw: Mapping[str, Any], *, base_dir: Path | None = None) -> AppConfig:
    if not isinstance(raw, Mapping):
        raise ConfigError("config must be an object")
    data = dict(raw)
    dex = str(data.get("dex", "")).strip()
    if not re.fullmatch(r"[a-z][a-z0-9]{0,5}", dex):
        raise ConfigError("dex must be 1-6 lowercase alphanumeric characters, starting with a letter")
    network = str(data.get("network", "testnet")).strip().lower()
    if network not in {"testnet", "mainnet"}:
        raise ConfigError("network must be 'testnet' or 'mainnet'")
    interval_ms = _integer(data.get("intervalMs", 3_000), "intervalMs")
    if interval_ms < 2_500:
        raise ConfigError("intervalMs must be at least 2500 for HIP-3 setOracle")
    mark_price_sets = _integer(data.get("markPriceSets", 0), "markPriceSets")
    if mark_price_sets not in {0, 1}:
        raise ConfigError("markPriceSets must be 0 or 1; duplicate inputs must not be presented as independent marks")
    dry_run = _boolean(data.get("dryRun", True), "dryRun")
    source_timeout_ms = _integer(data.get("sourceTimeoutMs", 2_000), "sourceTimeoutMs")
    publish_timeout_ms = _integer(data.get("publishTimeoutMs", 5_000), "publishTimeoutMs")
    confirmation_attempts = _integer(data.get("confirmationAttempts", 3), "confirmationAttempts")
    confirmation_interval_ms = _integer(data.get("confirmationIntervalMs", 200), "confirmationIntervalMs")
    if source_timeout_ms <= 0 or publish_timeout_ms <= 0:
        raise ConfigError("sourceTimeoutMs and publishTimeoutMs must be positive")
    if not 1 <= confirmation_attempts <= 10 or not 0 <= confirmation_interval_ms <= 1_000:
        raise ConfigError("confirmationAttempts must be 1-10 and confirmationIntervalMs must be 0-1000")

    raw_sources = _require_dict(data.get("sources"), "sources")
    if any(not isinstance(name, str) for name in raw_sources):
        raise ConfigError("source names must be strings")
    sources = {name: _parse_source(name, value) for name, value in raw_sources.items()}
    raw_feeds = data.get("feeds")
    if not isinstance(raw_feeds, list) or not raw_feeds:
        raise ConfigError("feeds must be a non-empty array")
    feeds = tuple(_parse_feed(index, value, sources) for index, value in enumerate(raw_feeds))
    coins = [feed.coin for feed in feeds]
    if len(coins) != len(set(coins)):
        raise ConfigError("feed coin names must be unique")
    if any(":" in coin and (not coin.startswith(f"{dex}:") or coin.count(":") != 1) for coin in coins):
        raise ConfigError("feed coin namespace must match dex")
    wire_coins = [coin if ":" in coin else f"{dex}:{coin}" for coin in coins]
    if len(wire_coins) != len(set(wire_coins)):
        raise ConfigError("feed coin names resolve to duplicate HIP-3 assets")
    if not dry_run:
        for source in sources.values():
            if source.kind == "static":
                raise ConfigError("live configurations cannot use static sources")
            if not source.options.get("timestampPath"):
                raise ConfigError(f"live source {source.name} requires timestampPath from the upstream")

    state_value = str(data.get("stateFile", "state/oracle-state.json")).strip()
    if not state_value:
        raise ConfigError("stateFile must not be empty")
    state_file = Path(state_value)
    if not state_file.is_absolute():
        state_file = (base_dir or Path.cwd()) / state_file
    mode = "dry-run" if dry_run else "live"
    state_file = state_file.with_name(
        f"{state_file.stem}.{network}.{mode}.{dex}{state_file.suffix or '.json'}"
    ).resolve()
    api_url = data.get("apiUrl")
    if (
        api_url
        and str(api_url).rstrip("/") != f"https://api.hyperliquid{'-testnet' if network == 'testnet' else ''}.xyz"
    ):
        raise ConfigError("apiUrl must match the official endpoint for the configured network")
    monitoring_raw = _require_dict(data.get("monitoring", {}), "monitoring")
    monitoring = MonitoringConfig(
        enabled=_boolean(monitoring_raw.get("enabled", False), "monitoring.enabled"),
        host=str(monitoring_raw.get("host", "127.0.0.1")),
        port=_integer(monitoring_raw.get("port", 9108), "monitoring.port"),
        readiness_max_age_ms=_integer(monitoring_raw.get("readinessMaxAgeMs", 10_000), "monitoring.readinessMaxAgeMs"),
    )
    if monitoring.host not in {"127.0.0.1", "0.0.0.0"} or not 0 <= monitoring.port <= 65535:
        raise ConfigError("monitoring.host must be 127.0.0.1 or 0.0.0.0 and port must be 0-65535")
    if monitoring.readiness_max_age_ms < interval_ms:
        raise ConfigError("monitoring.readinessMaxAgeMs must be at least intervalMs")
    return AppConfig(
        dex=dex,
        network=network,
        interval_ms=interval_ms,
        dry_run=dry_run,
        state_file=state_file,
        feeds=feeds,
        sources=sources,
        api_url=str(api_url).rstrip("/") if api_url else None,
        mark_price_sets=mark_price_sets,
        source_timeout_ms=source_timeout_ms,
        publish_timeout_ms=publish_timeout_ms,
        confirmation_attempts=confirmation_attempts,
        confirmation_interval_ms=confirmation_interval_ms,
        monitoring=monitoring,
    )


def load_config(path: str | Path) -> AppConfig:
    config_path = Path(path).expanduser().resolve()
    try:
        with config_path.open("r", encoding="utf-8") as handle:
            raw = json.load(handle)
    except (OSError, json.JSONDecodeError) as exc:
        raise ConfigError(f"cannot load config {config_path}: {exc}") from exc
    return parse_config(raw, base_dir=config_path.parent)
