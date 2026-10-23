import gzip
import json
import tempfile
from pathlib import Path
from unittest import mock

from django.core.management import call_command
from django.test import SimpleTestCase, TestCase

from accounts.models import Role, User


class HealthTests(TestCase):
    def test_answers_without_signing_in(self):
        r = self.client.get("/api/health/")
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json(), {"status": "ok", "database": "ok"})

    def test_a_monitor_can_use_head_and_the_page_is_never_cached(self):
        self.assertEqual(self.client.head("/api/health/").status_code, 200)
        self.assertIn("no-cache", self.client.get("/api/health/")["Cache-Control"])

    def test_only_safe_methods(self):
        self.assertEqual(self.client.post("/api/health/").status_code, 405)

    def test_says_so_when_the_database_is_down(self):
        with mock.patch("core.health.connection.cursor", side_effect=RuntimeError("down")):
            r = self.client.get("/api/health/")
        self.assertEqual((r.status_code, r.json()["status"]), (503, "error"))

    def test_reveals_nothing_about_the_system(self):
        self.assertEqual(set(self.client.get("/api/health/").json()), {"status", "database"})


class BackupTests(TestCase):
    def test_writes_a_compressed_file_with_the_data_and_without_secrets_of_sessions(self):
        User.objects.create_user(username="dan", email="dan@x.com", password="Str0ng!pass99", role=Role.STUDENT)
        with tempfile.TemporaryDirectory() as folder:
            call_command("backup_data", out=folder)
            files = list(Path(folder).glob("testprep-*.json.gz"))
            self.assertEqual(len(files), 1)
            with gzip.open(files[0], "rt", encoding="utf-8") as f:
                data = json.load(f)
        models = {row["model"] for row in data}
        self.assertIn("accounts.user", models)
        self.assertFalse({"sessions.session", "token_blacklist.outstandingtoken"} & models)
        self.assertTrue(any(row["fields"].get("username") == "dan" for row in data))


class SentrySettingTests(SimpleTestCase):
    def test_error_tracking_is_off_unless_a_dsn_is_set(self):
        import os

        self.assertFalse(os.getenv("SENTRY_DSN"))
