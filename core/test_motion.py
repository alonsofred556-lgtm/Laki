import os
from unittest.mock import patch

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "coni_backend.settings")

import django
from django.core.cache import cache
from django.test import SimpleTestCase, override_settings

django.setup()

from .motion import MotionError, discoveries, request_json


class MotionDiscoveryTests(SimpleTestCase):
    def setUp(self):
        cache.clear()

    def tearDown(self):
        cache.clear()

    @override_settings(CONI_MOTION_ENABLED=False, CONI_MOTION_API_KEY="")
    def test_disabled_motion_feed_returns_empty_read_only_payload(self):
        with patch("core.motion._SESSION.get") as request:
            result = discoveries()
        self.assertFalse(result["enabled"])
        self.assertEqual(result["rows"], [])
        request.assert_not_called()

    @override_settings(
        CONI_MOTION_ENABLED=True,
        CONI_MOTION_API_KEY="msk_" + "a" * 43,
        CONI_MOTION_API_URL="https://motion.gift/api/v1",
        CONI_MOTION_CACHE_SECONDS=300,
    )
    def test_discovery_normalizes_supported_motion_chains_and_keeps_key_server_side(self):
        response = type("Response", (), {
            "ok": True,
            "status_code": 200,
            "json": lambda self: {"ok": True, "data": {"rows": [{"address": "0x" + "1" * 40, "symbol": "TEST", "priceUsd": 1.2, "mcapUsd": 42}], "meta": {}}, "meta": {"chain": "bsc", "at": 123}},
        })()
        with patch("core.motion._SESSION.get", return_value=response) as request:
            result = discoveries(chain="bsc", limit=1)
        self.assertTrue(result["enabled"])
        self.assertEqual(result["rows"][0]["chain"], "bsc")
        self.assertEqual(result["rows"][0]["motionChain"], "bsc")
        self.assertEqual(result["rows"][0]["symbol"], "TEST")
        self.assertNotIn("apiKey", result["rows"][0])
        self.assertEqual(request.call_args.kwargs["headers"]["Authorization"], "Bearer " + "msk_" + "a" * 43)
        self.assertNotIn("msk_", str(result))

    @override_settings(CONI_MOTION_ENABLED=True, CONI_MOTION_API_KEY="msk_" + "b" * 43)
    def test_request_requires_https_and_marks_upstream_failures(self):
        with override_settings(CONI_MOTION_API_URL="http://motion.gift/api/v1"):
            with self.assertRaises(MotionError):
                request_json("/trending")

    @override_settings(CONI_MOTION_ENABLED=True, CONI_MOTION_API_KEY="msk_" + "c" * 43)
    def test_stale_cached_rows_are_served_when_motion_is_unavailable(self):
        with patch("core.motion._SESSION.get", side_effect=MotionError("offline")):
            cache.set("coni:motion:trending:robinhood:stale", [{"chain": "robinhood", "symbol": "OLD"}], 3600)
            result = discoveries(chain="robinhood", limit=1)
        self.assertEqual(result["rows"][0]["symbol"], "OLD")
