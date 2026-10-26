import json
import hashlib
import hmac
import os
import time
from types import SimpleNamespace
from unittest.mock import patch

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "coni_backend.settings")

import django
from django.test import RequestFactory, SimpleTestCase, override_settings

django.setup()

from .views import signer_bot_wallet_provision_requests, signer_callback_allowed


class SignerCallbackTests(SimpleTestCase):
    @override_settings(CONI_SIGNER_CALLBACK_REQUIRE_HMAC=True, CONI_SIGNER_CALLBACK_MAX_SKEW_SECONDS=60)
    def test_callback_requires_hmac_over_exact_body(self):
        secret = "callback-secret"
        payload = b'{"executionId":"abc"}'
        timestamp = str(int(time.time()))
        signature = hmac.new(secret.encode(), f"{timestamp}.".encode() + payload, hashlib.sha256).hexdigest()
        request = RequestFactory().post("/internal", data=payload, content_type="application/json", HTTP_X_CONI_SIGNER_TIMESTAMP=timestamp, HTTP_X_CONI_SIGNER_SIGNATURE=signature)
        self.assertTrue(signer_callback_allowed(request, secret, "X-Legacy"))

        altered = RequestFactory().post("/internal", data=b'{"executionId":"other"}', content_type="application/json", HTTP_X_CONI_SIGNER_TIMESTAMP=timestamp, HTTP_X_CONI_SIGNER_SIGNATURE=signature)
        self.assertFalse(signer_callback_allowed(altered, secret, "X-Legacy"))

    @override_settings(CONI_SIGNER_CALLBACK_REQUIRE_HMAC=True, CONI_SIGNER_CALLBACK_MAX_SKEW_SECONDS=60)
    def test_old_callback_is_rejected(self):
        payload = b"{}"
        timestamp = str(int(time.time()) - 120)
        signature = hmac.new(b"callback-secret", f"{timestamp}.".encode() + payload, hashlib.sha256).hexdigest()
        request = RequestFactory().post("/internal", data=payload, content_type="application/json", HTTP_X_CONI_SIGNER_TIMESTAMP=timestamp, HTTP_X_CONI_SIGNER_SIGNATURE=signature)
        self.assertFalse(signer_callback_allowed(request, "callback-secret", "X-Legacy"))

    def test_provision_pull_returns_only_public_wallet_metadata(self):
        request = RequestFactory().get("/internal/signer/bot-wallet-provision-requests?limit=1")
        wallet = SimpleNamespace(
            id="wallet-id",
            owner_id="owner-id",
            chain="base",
            mode="external-bot",
            source_address="0x1111111111111111111111111111111111111111",
        )
        item = SimpleNamespace(
            id="request-id",
            bot_wallet_id="wallet-id",
            bot_wallet=wallet,
            attempt_count=1,
            lease_until=None,
        )
        with patch("core.views.signer_provision_allowed", return_value=True), patch("core.views.claim_provision_requests", return_value=[item]):
            response = signer_bot_wallet_provision_requests(request)

        self.assertEqual(response.status_code, 200)
        payload = json.loads(response.content)
        self.assertEqual(payload["requests"][0]["chain"], "base")
        self.assertNotIn("privateKey", payload["requests"][0])
        self.assertNotIn("seed", payload["requests"][0])
