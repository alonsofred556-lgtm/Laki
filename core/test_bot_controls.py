import json
import os
from types import SimpleNamespace
from unittest.mock import patch

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "coni_backend.settings")

import django
from django.test import SimpleTestCase

django.setup()

from .bot_controls import effective_user_policy
from .views import user_bot_config


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


class UserBotConfigTests(SimpleTestCase):
    @patch("core.views.user_bot_status", return_value={"walletMode": "internal-bot"})
    @patch("core.views.ensure_bot_wallets")
    @patch("core.views.rate_limited", return_value=None)
    @patch("core.views.post_allowed", return_value=True)
    @patch("core.views.body", return_value={"walletMode": "internal-bot"})
    @patch("core.views.global_bot_control")
    @patch("core.views.ensure_user_bot_control")
    @patch("core.views.require_session")
    def test_switching_mode_provisions_records_immediately(
        self,
        require_session,
        ensure_user_control,
        global_control,
        body,
        post_allowed,
        rate_limited,
        ensure_wallets,
        user_status,
    ):
        identity = SimpleNamespace(id="identity-1")
        control = SimpleNamespace(
            wallet_mode="external-bot",
            enabled=True,
            paused=False,
            min_net_bps=100,
            max_notional_usd=25,
            compound_profit_bps=7000,
            save=lambda: None,
        )
        require_session.return_value = identity
        ensure_user_control.return_value = control
        global_control.return_value = SimpleNamespace(
            min_net_bps=100,
            max_notional_usd=25,
            compound_profit_bps=7000,
        )

        from django.test import RequestFactory

        request = RequestFactory().post(
            "/api/bot/control",
            data=json.dumps({"walletMode": "internal-bot"}),
            content_type="application/json",
        )
        response = user_bot_config(request)

        self.assertEqual(response.status_code, 200)
        ensure_wallets.assert_called_once_with(identity, mode="internal-bot")
        self.assertEqual(control.wallet_mode, "internal-bot")
