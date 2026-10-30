import os
from unittest.mock import patch

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "coni_backend.settings")

import django

django.setup()

from django.test import RequestFactory, SimpleTestCase

from . import views


class MarketFeedRelayTests(SimpleTestCase):
    def setUp(self):
        self.factory = RequestFactory()

    @patch("core.views.pangea_proxy")
    def test_history_without_launch_requests_all_launches(self, proxy):
        proxy.return_value = object()
        response = views.market_history(self.factory.get("/api/market/history?hours=24"))
        self.assertIs(response, proxy.return_value)
        proxy.assert_called_once()
        self.assertEqual(proxy.call_args.args[1:], ("/history", "hours=24"))

    @patch("core.views.pangea_proxy")
    def test_history_with_launch_keeps_the_filter(self, proxy):
        proxy.return_value = object()
        response = views.market_history(self.factory.get("/api/market/history?launch=demo&hours=24"))
        self.assertIs(response, proxy.return_value)
        proxy.assert_called_once()
        self.assertEqual(proxy.call_args.args[1:], ("/history", "hours=24&launch=demo"))

    @patch("core.views.pangea_proxy")
    def test_bridge_activity_without_token_requests_all_tokens(self, proxy):
        proxy.return_value = object()
        response = views.bridge_activity(self.factory.get("/api/market/bridge-activity?hours=24&limit=25"))
        self.assertIs(response, proxy.return_value)
        proxy.assert_called_once()
        self.assertEqual(proxy.call_args.args[1:], ("/bridge/activity", "hours=24&limit=25"))

    @patch("core.views.pangea_proxy")
    def test_bridge_activity_with_token_keeps_the_filter(self, proxy):
        proxy.return_value = object()
        response = views.bridge_activity(self.factory.get("/api/market/bridge-activity?token=demo&hours=24&limit=25"))
        self.assertIs(response, proxy.return_value)
        proxy.assert_called_once()
        self.assertEqual(proxy.call_args.args[1:], ("/bridge/activity", "hours=24&limit=25&token=demo"))
