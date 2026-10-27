import json
import os
from types import SimpleNamespace
from unittest.mock import patch

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "coni_backend.settings")

import django
from django.test import RequestFactory, SimpleTestCase, override_settings

django.setup()

from .views import funding_assets


class FundingAssetEndpointTests(SimpleTestCase):
    def setUp(self):
        self.factory = RequestFactory()
        self.identity = SimpleNamespace(id="identity-1", chain="base", address="0x1111111111111111111111111111111111111111")

    @override_settings(
        CONI_TOKEN_REGISTRY=json.dumps({
            "ZGAP": {"base": {"address": "0x2222222222222222222222222222222222222222", "decimals": 9}},
            "UNVERIFIED": {"base": {"address": "0x3333333333333333333333333333333333333333", "decimals": None}},
        })
    )
    @patch("core.views.require_session")
    def test_returns_native_and_verified_token_assets_only(self, require_session):
        require_session.return_value = self.identity
        response = funding_assets(self.factory.get("/api/funding/assets?chain=base"))
        self.assertEqual(response.status_code, 200)
        payload = json.loads(response.content)
        self.assertEqual(payload["chain"], "base")
        self.assertEqual([item["asset"] for item in payload["assets"]], ["ETH", "ZGAP"])
        self.assertEqual(payload["assets"][1]["tokenAddress"], "0x2222222222222222222222222222222222222222")
        self.assertEqual(payload["assets"][1]["decimals"], 9)

    @patch("core.views.require_session")
    def test_rejects_chain_not_accessible_to_authenticated_identity(self, require_session):
        require_session.return_value = self.identity
        response = funding_assets(self.factory.get("/api/funding/assets?chain=solana"))
        self.assertEqual(response.status_code, 403)
