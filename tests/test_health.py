import os
from unittest.mock import patch

from django.db import DatabaseError
from django.test import TestCase


class ReadinessTests(TestCase):
    def test_reports_running_release_and_disables_cache(self):
        with patch.dict(os.environ, {"FISHSITE_RELEASE_SHA": "a" * 40}):
            response = self.client.get("/healthz/")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), {"status": "ok", "sha": "a" * 40})
        self.assertIn("no-store", response["Cache-Control"])

    def test_database_failure_is_not_healthy_or_disclosed(self):
        with patch("fishsite.health.connection.cursor", side_effect=DatabaseError("credential-marker")):
            response = self.client.get("/healthz/")
        self.assertEqual(response.status_code, 503)
        self.assertEqual(response.json(), {"status": "unavailable"})
