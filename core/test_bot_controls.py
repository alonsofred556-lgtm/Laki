import os
from types import SimpleNamespace
from unittest.mock import patch

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "coni_backend.settings")

import django
from django.test import SimpleTestCase

django.setup()

from .bot_controls import effective_user_policy


class UserBotPolicyTests(SimpleTestCase):
    @patch("core.bot_controls.ensure_user_bot_control")
    def test_user_policy_can_only_be_more_conservative_than_global(self, ensure):
        ensure.return_value = SimpleNamespace(
            wallet_mode="internal-bot",
            enabled=True,
            paused=False,
            min_net_bps=250,
            max_notional_usd="10",
        )
        result = effective_user_policy(
            SimpleNamespace(),
            SimpleNamespace(enabled=True, paused=False, min_net_bps=100, max_notional_usd="25"),
        )
        self.assertEqual(result["walletMode"], "internal-bot")
        self.assertEqual(result["minNetBps"], 250)
        self.assertEqual(result["maxNotionalUsd"], 10.0)

    @patch("core.bot_controls.ensure_user_bot_control")
    def test_global_policy_wins_when_user_is_more_permissive(self, ensure):
        ensure.return_value = SimpleNamespace(
            wallet_mode="external-bot",
            enabled=True,
            paused=False,
            min_net_bps=10,
            max_notional_usd="100",
        )
        result = effective_user_policy(
            SimpleNamespace(),
            SimpleNamespace(enabled=False, paused=True, min_net_bps=100, max_notional_usd="25"),
        )
        self.assertEqual(result["minNetBps"], 100)
        self.assertEqual(result["maxNotionalUsd"], 25.0)
        self.assertFalse(result["globalEnabled"])
        self.assertTrue(result["globalPaused"])
