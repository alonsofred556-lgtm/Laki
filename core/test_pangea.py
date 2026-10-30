import os
from unittest.mock import patch

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "coni_backend.settings")

import django
from django.core.cache import cache
from django.test import SimpleTestCase

django.setup()

from .pangea import PangeaError, _image_url, market_snapshot, token_registry_snapshot


class PangeaTokenFeedTests(SimpleTestCase):
    def setUp(self):
        cache.clear()

    def tearDown(self):
        cache.clear()

    def test_token_snapshot_does_not_block_on_uncached_metadata(self):
        pools = {
            "readAt": "2026-10-26T00:00:00Z",
            "launches": [{"slug": "demo", "name": "Demo", "symbol": "D", "pools": []}],
        }
        launch_feed = {"launches": []}
        with patch("core.pangea.request_json", side_effect=[pools, launch_feed]) as request:
            result = token_registry_snapshot()

        self.assertEqual(len(result), 1)
        self.assertEqual(request.call_count, 2)
        self.assertFalse(any("/token/meta" in str(call) for call in request.call_args_list))
        self.assertNotIn("pools", result[0])

    def test_relative_pangea_logo_path_is_normalized_safely(self):
        self.assertEqual(
            _image_url("/api/logo/QmExample/128.webp"),
            "https://www.pangea.trade/api/logo/QmExample/128.webp",
        )
        self.assertIsNone(_image_url("/other-host/logo.webp"))

    def test_token_snapshot_serves_last_known_good_data_on_pangea_outage(self):
        stale = [{"slug": "cached", "symbol": "CACHED", "name": "Cached", "pools": []}]
        cache.set("coni:pangea:tokens:stale", stale, 3600)
        with patch("core.pangea.request_json", side_effect=PangeaError("upstream timeout")):
            result = token_registry_snapshot()

        self.assertEqual(result, stale)

    def test_launch_enrichment_uses_a_bounded_timeout(self):
        pools = {
            "readAt": "2026-10-26T00:00:00Z",
            "launches": [{"slug": "demo", "name": "Demo", "symbol": "D", "pools": []}],
        }
        with patch("core.pangea.request_json", side_effect=[pools, PangeaError("launch feed timeout")]) as request:
            result = token_registry_snapshot()

        self.assertEqual(len(result), 1)
        self.assertEqual(request.call_args_list[1].kwargs["timeout"], 2)

    def test_market_snapshot_merges_new_launch_metadata_without_losing_live_pools(self):
        pools = {
            "readAt": "2026-10-26T00:00:00Z",
            "rates": {"ETH": "3000"},
            "launches": [{
                "slug": "new-token",
                "symbol": "NEW",
                "pools": [{"chain": "base", "token": "0xpool", "priceUsd": "1"}],
            }],
        }
        feed = {"launches": [{"slug": "new-token", "name": "New Token", "image": "https://img.example/new.webp"}]}
        with patch("core.pangea.request_json", side_effect=[pools, feed]):
            result = market_snapshot()

        self.assertEqual(result["launches"][0]["name"], "New Token")
        self.assertEqual(result["launches"][0]["pools"][0]["token"], "0xpool")
        self.assertEqual(result["rates"], {"ETH": "3000"})

    def test_market_snapshot_reuses_short_lived_launch_metadata_cache(self):
        pools = {
            "readAt": "2026-10-26T00:00:00Z",
            "launches": [{"slug": "cached-token", "symbol": "CACHE", "pools": []}],
        }
        feed = {"launches": [{"slug": "cached-token", "name": "Cached Token"}]}
        with patch("core.pangea.request_json", side_effect=[pools, feed, pools]) as request:
            first = market_snapshot()
            second = market_snapshot()

        self.assertEqual(first["launches"][0]["name"], "Cached Token")
        self.assertEqual(second["launches"][0]["name"], "Cached Token")
        self.assertEqual(request.call_count, 3)
        self.assertEqual(request.call_args_list[1].args[0], "/v1/launches?limit=100")
