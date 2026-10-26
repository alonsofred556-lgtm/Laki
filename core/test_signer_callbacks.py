import hashlib
import hmac
import os
import time

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "coni_backend.settings")

import django
from django.test import RequestFactory, SimpleTestCase, override_settings

django.setup()

from .views import signer_callback_allowed


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
