from __future__ import annotations

import hashlib
import json
import os
import tempfile
import time
from dataclasses import asdict, dataclass
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any, Mapping


class StateError(RuntimeError):
    pass


@dataclass(slots=True)
class FeedState:
    last_price: str
    last_observed_at_ms: int
    last_published_at_ms: int


class JsonStateStore:
    def __init__(self, path: Path, *, scope: Mapping[str, str] | None = None):
        self.path = path
        self.scope = dict(scope) if scope is not None else None
        self.pending: dict[str, Any] | None = None
        self.last_attempt_at_ms = 0
        self._feeds: dict[str, FeedState] = {}
        self._load()

    def _load(self) -> None:
        if not self.path.exists():
            return
        try:
            with self.path.open("r", encoding="utf-8") as handle:
                raw = json.load(handle)
            if not isinstance(raw, dict):
                raise StateError("state root must be an object")
            if raw.get("version") not in {1, 2} or not isinstance(raw.get("feeds"), dict):
                raise StateError("unsupported state format")
            if self.scope is not None and raw.get("scope") != self.scope:
                raise StateError("state scope mismatch or legacy state: explicit migration is required")
            self.pending = raw.get("pending")
            self.last_attempt_at_ms = int(raw.get("lastAttemptAtMs", 0))
            if self.last_attempt_at_ms < 0:
                raise StateError("invalid last attempt timestamp")
            if self.pending is not None:
                if not isinstance(self.pending, dict) or not isinstance(self.pending.get("feeds"), dict):
                    raise StateError("invalid pending publication")
                if not isinstance(self.pending.get("action"), dict) or not isinstance(self.pending.get("id"), str):
                    raise StateError("invalid pending publication")
                for value in self.pending["feeds"].values():
                    price = Decimal(str(value["last_price"]))
                    if not price.is_finite() or price <= 0 or int(value["last_observed_at_ms"]) < 0:
                        raise StateError("invalid pending price or timestamp")
            for coin, value in raw["feeds"].items():
                price = Decimal(str(value["last_price"]))
                if not price.is_finite() or price <= 0:
                    raise StateError(f"invalid stored price for {coin}")
                observed_at_ms = int(value["last_observed_at_ms"])
                published_at_ms = int(value["last_published_at_ms"])
                if observed_at_ms < 0 or published_at_ms < 0:
                    raise StateError(f"invalid stored timestamp for {coin}")
                self._feeds[coin] = FeedState(
                    last_price=str(price),
                    last_observed_at_ms=observed_at_ms,
                    last_published_at_ms=published_at_ms,
                )
        except (OSError, json.JSONDecodeError, KeyError, TypeError, ValueError, InvalidOperation) as exc:
            raise StateError(f"cannot read state file {self.path}: {exc}") from exc

    def previous_price(self, coin: str) -> Decimal | None:
        state = self._feeds.get(coin)
        return Decimal(state.last_price) if state else None

    def reload(self) -> None:
        loaded = JsonStateStore(self.path, scope=self.scope)
        self._feeds = loaded._feeds
        self.pending = loaded.pending
        self.last_attempt_at_ms = loaded.last_attempt_at_ms

    def last_published_at_ms(self) -> int:
        return max(
            self.last_attempt_at_ms, max((value.last_published_at_ms for value in self._feeds.values()), default=0)
        )

    def prepare(self, action: dict[str, Any], values: Mapping[str, tuple[Decimal, int]]) -> None:
        if self.pending is not None:
            raise StateError("unresolved pending publication; reconcile before publishing again")
        now_ms = int(time.time() * 1000)
        self.last_attempt_at_ms = now_ms
        body = json.dumps(action, sort_keys=True, separators=(",", ":"))
        self.pending = {
            "id": hashlib.sha256(f"{now_ms}:{body}".encode()).hexdigest(),
            "preparedAtMs": now_ms,
            "action": action,
            "feeds": {
                coin: asdict(FeedState(str(price), observed_at_ms, now_ms))
                for coin, (price, observed_at_ms) in values.items()
            },
        }
        try:
            self.save()
        except StateError:
            # In memory also remains blocked: an fsync/replace error can be ambiguous.
            raise

    def commit(self, values: Mapping[str, tuple[Decimal, int]], published_at_ms: int) -> None:
        old_feeds = self._feeds.copy()
        old_pending = self.pending
        try:
            for coin, (price, observed_at_ms) in values.items():
                self.update(coin, price, observed_at_ms, published_at_ms)
            self.pending = None
            self.save()
        except StateError:
            self._feeds = old_feeds
            self.pending = old_pending
            raise

    def clear_pending(self) -> None:
        old_pending = self.pending
        self.pending = None
        try:
            self.save()
        except StateError:
            self.pending = old_pending
            raise

    def update(self, coin: str, price: Decimal, observed_at_ms: int, published_at_ms: int) -> None:
        if not price.is_finite() or price <= 0:
            raise StateError(f"refusing invalid state price for {coin}")
        if observed_at_ms < 0 or published_at_ms < 0:
            raise StateError(f"refusing invalid state timestamp for {coin}")
        self._feeds[coin] = FeedState(str(price), observed_at_ms, published_at_ms)

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "version": 2,
            "scope": self.scope,
            "pending": self.pending,
            "lastAttemptAtMs": self.last_attempt_at_ms,
            "feeds": {coin: asdict(state) for coin, state in sorted(self._feeds.items())},
        }
        temporary_path: str | None = None
        try:
            with tempfile.NamedTemporaryFile(
                "w", encoding="utf-8", dir=self.path.parent, prefix=f".{self.path.name}.", delete=False
            ) as handle:
                json.dump(payload, handle, indent=2, sort_keys=True)
                handle.write("\n")
                handle.flush()
                os.fsync(handle.fileno())
                temporary_path = handle.name
            os.replace(temporary_path, self.path)
            directory_fd = os.open(self.path.parent, os.O_RDONLY)
            try:
                os.fsync(directory_fd)
            finally:
                os.close(directory_fd)
        except OSError as exc:
            if temporary_path:
                try:
                    os.unlink(temporary_path)
                except OSError:
                    pass
            raise StateError(f"cannot persist state file {self.path}: {exc}") from exc
