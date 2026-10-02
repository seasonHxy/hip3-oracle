"""One writer per local state file. This does not fence multiple hosts."""

from __future__ import annotations

import fcntl
import os
from pathlib import Path

from .state import StateError


class WriterLock:
    def __init__(self, state_path: Path):
        self.path = state_path.with_suffix(state_path.suffix + ".lock")
        self._fd: int | None = None

    def __enter__(self) -> "WriterLock":
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._fd = os.open(self.path, os.O_CREAT | os.O_RDWR, 0o600)
        try:
            fcntl.flock(self._fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            os.close(self._fd)
            self._fd = None
            raise StateError("another local writer holds this state lock") from None
        return self

    def __exit__(self, *_args: object) -> None:
        if self._fd is not None:
            os.close(self._fd)
            self._fd = None
