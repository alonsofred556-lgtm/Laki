import os
from unittest.mock import patch

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "coni_backend.settings")

import django
from django.core.cache import cache
from django.test import SimpleTestCase

django.setup()

from .pangea import token_registry_snapshot


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
