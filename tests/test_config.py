import json
import unittest
from pathlib import Path

from hip3_oracle.config import ConfigError, load_config, parse_config

ROOT = Path(__file__).resolve().parents[1]


class ConfigTests(unittest.TestCase):
    def raw(self):
        return json.loads((ROOT / "config" / "example.json").read_text())

    def test_modes_networks_and_dex_have_separate_state_files(self) -> None:
        raw = self.raw()
        dry_testnet = parse_config(raw).state_file
        raw["network"] = "mainnet"
        dry_mainnet = parse_config(raw).state_file
        raw["dryRun"] = False
        for source in raw["sources"].values():
            source.update(kind="json", url="https://provider.example", pricePath="px", timestampPath="ts")
        live_mainnet = parse_config(raw).state_file
        raw["dex"] = "other"
        other_dex = parse_config(raw).state_file
        self.assertEqual(len({dry_testnet, dry_mainnet, live_mainnet, other_dex}), 4)

    def test_rejects_concentrated_config(self) -> None:
        raw = self.raw()
        raw["sources"]["venue-a"]["weight"] = "9"
        with self.assertRaisesRegex(ConfigError, "maxGroupWeightShareBps"):
            parse_config(raw)

    def test_rejects_live_static_sources(self) -> None:
        raw = self.raw()
        raw["dryRun"] = False
        with self.assertRaisesRegex(ConfigError, "static"):
            parse_config(raw)

    def test_live_source_requires_upstream_timestamp(self) -> None:
        raw = self.raw()
        raw["dryRun"] = False
        for source in raw["sources"].values():
            source["kind"] = "json"
        with self.assertRaisesRegex(ConfigError, "timestampPath"):
            parse_config(raw)

    def test_rejects_network_endpoint_mismatch(self) -> None:
        raw = self.raw()
        raw["apiUrl"] = "https://api.hyperliquid.xyz"
        with self.assertRaisesRegex(ConfigError, "official endpoint"):
            parse_config(raw)

    def test_rejects_fractional_integer_field(self) -> None:
        raw = self.raw()
        raw["intervalMs"] = 3000.8
        with self.assertRaisesRegex(ConfigError, "integer"):
            parse_config(raw)

    def test_rejects_duplicate_resolved_coin_names(self) -> None:
        raw = self.raw()
        raw["feeds"].append({**raw["feeds"][0], "coin": "demo:AAPL"})
        with self.assertRaisesRegex(ConfigError, "duplicate HIP-3"):
            parse_config(raw)

    def test_example_configuration_loads(self) -> None:
        config = load_config(ROOT / "config" / "example.json")
        self.assertEqual(config.dex, "demo")
        self.assertEqual(config.feeds[0].coin, "AAPL")
        self.assertTrue(config.dry_run)

    def test_rejects_interval_below_protocol_limit(self) -> None:
        with self.assertRaisesRegex(ConfigError, "2500"):
            parse_config({"dex": "demo", "intervalMs": 2499, "sources": {}, "feeds": []})

    def test_rejects_false_independence_quorum(self) -> None:
        raw = {
            "dex": "demo",
            "sources": {
                "a": {"kind": "static", "independenceGroup": "same", "prices": {"X": "1"}},
                "b": {"kind": "static", "independenceGroup": "same", "prices": {"X": "1"}},
            },
            "feeds": [
                {
                    "coin": "X",
                    "szDecimals": 2,
                    "sources": ["a", "b"],
                    "minSources": 2,
                    "minIndependentGroups": 2,
                }
            ],
        }
        with self.assertRaisesRegex(ConfigError, "independent source groups"):
            parse_config(raw)

    def test_rejects_string_boolean(self) -> None:
        with self.assertRaisesRegex(ConfigError, "boolean"):
            parse_config({"dex": "demo", "dryRun": "false", "sources": {}, "feeds": []})


if __name__ == "__main__":
    unittest.main()
