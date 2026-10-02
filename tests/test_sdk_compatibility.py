"""Exercise the pinned SDK's real signing code without making a network call."""

import importlib.util
import unittest
from unittest.mock import Mock, patch

import test_publisher

from hip3_oracle.publisher import SdkPublisher


@unittest.skipUnless(importlib.util.find_spec("hyperliquid"), "install .[live] for SDK compatibility tests")
class SdkCompatibilityTests(unittest.TestCase):
    def test_exchange_initialization_passes_dex_and_timeout_without_network(self):
        from eth_account import Account

        account = Account.create()  # Ephemeral, unfunded wallet for an offline test only.
        publisher = SdkPublisher(test_publisher.PayloadTests().config(), private_key=account.key.hex())
        with patch("hyperliquid.exchange.Info") as info:
            exchange = publisher._get_exchange()
        self.assertEqual(info.call_args.args[4], ["demo"])
        self.assertEqual(info.call_args.args[5], 5.0)
        self.assertEqual(exchange.base_url, "https://api.hyperliquid-testnet.xyz")
        self.assertEqual(publisher.private_key, "")

    def test_official_set_oracle_signs_sorted_action_with_mocked_transport(self):
        from eth_account import Account
        from hyperliquid.exchange import Exchange

        exchange = Exchange.__new__(Exchange)
        exchange.wallet = Account.create()
        exchange.base_url = "https://api.hyperliquid-testnet.xyz"
        exchange.expires_after = None
        exchange._post_action = Mock(return_value={"status": "ok"})
        result = exchange.perp_deploy_set_oracle(
            "demo", {"demo:TSLA": "390.22", "demo:AAPL": "230.2"}, [], {"demo:TSLA": "390.22", "demo:AAPL": "230.2"}
        )
        self.assertEqual(result["status"], "ok")
        action, signature, nonce = exchange._post_action.call_args.args
        self.assertEqual(action["setOracle"]["oraclePxs"], [("demo:AAPL", "230.2"), ("demo:TSLA", "390.22")])
        self.assertEqual(signature["v"] in (27, 28), True)
        self.assertIsInstance(nonce, int)
