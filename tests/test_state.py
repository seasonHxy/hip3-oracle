import unittest
from decimal import Decimal
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from hip3_oracle.state import JsonStateStore, StateError


class StateTests(unittest.TestCase):
    def test_cross_environment_state_cannot_be_loaded(self) -> None:
        with TemporaryDirectory() as directory:
            path = Path(directory) / "state.json"
            state = JsonStateStore(path, scope={"mode": "dry-run"})
            state.save()
            with self.assertRaisesRegex(StateError, "scope mismatch"):
                JsonStateStore(path, scope={"mode": "live"})

    def test_legacy_unscoped_file_needs_explicit_migration(self) -> None:
        with TemporaryDirectory() as directory:
            path = Path(directory) / "state.json"
            path.write_text('{"version":1,"feeds":{}}')
            with self.assertRaisesRegex(StateError, "migration"):
                JsonStateStore(path, scope={"mode": "live"})

    def test_atomic_replace_failure_preserves_previous_durable_state(self) -> None:
        with TemporaryDirectory() as directory:
            path = Path(directory) / "state.json"
            state = JsonStateStore(path)
            state.commit({"AAPL": (Decimal("230.2"), 100)}, 200)
            with patch("hip3_oracle.state.os.replace", side_effect=OSError("disk full")):
                with self.assertRaises(StateError):
                    state.commit({"AAPL": (Decimal("240"), 300)}, 400)
            self.assertEqual(state.previous_price("AAPL"), Decimal("230.2"))
            self.assertEqual(JsonStateStore(path).previous_price("AAPL"), Decimal("230.2"))

    def test_state_round_trip(self) -> None:
        with TemporaryDirectory() as directory:
            path = Path(directory) / "state.json"
            state = JsonStateStore(path)
            state.update("AAPL", Decimal("230.2"), 100, 200)
            state.save()
            loaded = JsonStateStore(path)
            self.assertEqual(loaded.previous_price("AAPL"), Decimal("230.2"))

    def test_corrupt_state_fails_closed(self) -> None:
        with TemporaryDirectory() as directory:
            path = Path(directory) / "state.json"
            path.write_text("not json", encoding="utf-8")
            with self.assertRaises(StateError):
                JsonStateStore(path)

    def test_non_finite_state_fails_closed(self) -> None:
        with TemporaryDirectory() as directory:
            path = Path(directory) / "state.json"
            path.write_text(
                '{"version":1,"feeds":{"AAPL":{"last_price":"NaN","last_observed_at_ms":1,"last_published_at_ms":2}}}',
                encoding="utf-8",
            )
            with self.assertRaises(StateError):
                JsonStateStore(path)


if __name__ == "__main__":
    unittest.main()
