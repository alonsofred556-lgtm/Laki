import json
import os
from unittest.mock import patch

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "coni_backend.settings")

import django
from django.test import RequestFactory, SimpleTestCase

django.setup()

from .views import user_activity


class UserActivityTests(SimpleTestCase):
    def test_activity_requires_an_authenticated_wallet_session(self):
        request = RequestFactory().get("/api/activity")
        with patch("core.views.session_identities", return_value=[]):
            response = user_activity(request)
        self.assertEqual(response.status_code, 401)
        self.assertEqual(json.loads(response.content), {"error": "authenticated wallet session required"})
