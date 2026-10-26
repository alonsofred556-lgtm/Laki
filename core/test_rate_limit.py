import os
from unittest.mock import patch

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "coni_backend.settings")

import django
from django.test import SimpleTestCase, override_settings

django.setup()

from .rate_limit import RateLimitUnavailable, _client, allow


class RateLimitTests(SimpleTestCase):
    def tearDown(self):
        _client.cache_clear()

    @override_settings(CONI_REDIS_URL="", CONI_REDIS_REQUIRED=True)
    def test_required_redis_fails_closed(self):
        with self.assertRaises(RateLimitUnavailable):
            allow("test", "client")

    @override_settings(CONI_REDIS_URL="", CONI_REDIS_REQUIRED=False)
    def test_local_development_can_use_unlimited_fallback(self):
        self.assertEqual(allow("test", "client", limit=3), (True, 3))

    def test_redis_errors_fail_closed(self):
        class BrokenClient:
            def pipeline(self):
                raise RuntimeError("redis unavailable")

        with patch("core.rate_limit._client", return_value=BrokenClient()), patch("django.conf.settings.CONI_REDIS_REQUIRED", True):
            with self.assertRaises(RateLimitUnavailable):
                allow("test", "client")
