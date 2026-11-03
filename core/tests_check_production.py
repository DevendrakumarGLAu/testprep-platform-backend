from io import StringIO

from django.core.management import call_command
from django.core.management.base import CommandError
from django.test import TestCase, override_settings

from accounts.models import Role, User
from core.management.commands.check_production import run_checks

GOOD = dict(
    DEBUG=False, SECRET_KEY="k" * 50, ALLOWED_HOSTS=["api.example.com"], CORS_ALLOWED_ORIGINS=["https://app.example.com"],
    FRONTEND_URL="https://app.example.com", SECURE_SSL_REDIRECT=True, SESSION_COOKIE_SECURE=True,
)


def level_of(results, start):
    return next(lv for lv, what, _ in results if what.startswith(start))


class CheckProductionTests(TestCase):
    @override_settings(DEBUG=True, SECRET_KEY="insecure-dev-key-change-me", ALLOWED_HOSTS=["localhost"], CORS_ALLOWED_ORIGINS=["http://localhost:4200"], FRONTEND_URL="http://localhost:4201")
    def test_a_development_setup_fails_the_checks_that_matter(self):
        r = run_checks()
        self.assertEqual(level_of(r, "Debug mode is on"), "FAIL")
        self.assertEqual(level_of(r, "The secret key"), "FAIL")
        self.assertEqual(level_of(r, "Allowed hosts are only local"), "FAIL")
        self.assertEqual(level_of(r, "Only local web addresses"), "FAIL")
        self.assertEqual(level_of(r, "FRONTEND_URL points"), "WARN")

    @override_settings(**GOOD)
    def test_good_settings_pass(self):
        r = run_checks()
        for start in ("Debug mode is off", "The secret key is long", "Allowed hosts: api.example.com", "The API accepts calls", "FRONTEND_URL is", "HTTPS is enforced"):
            self.assertEqual(level_of(r, start), "OK", start)

    @override_settings(**{**GOOD, "ALLOWED_HOSTS": ["*"], "CORS_ALLOWED_ORIGINS": ["http://app.example.com"]})
    def test_wildcards_and_plain_http_are_warnings(self):
        r = run_checks()
        self.assertEqual(level_of(r, "Allowed hosts accept any"), "WARN")
        self.assertEqual(level_of(r, "A web address in CORS"), "WARN")

    def test_the_test_database_is_sqlite_so_that_check_fails(self):
        self.assertEqual(level_of(run_checks(), "The database is a local SQLite"), "FAIL")

    def test_demo_accounts_with_known_passwords_fail_and_a_changed_password_passes(self):
        User.objects.create_user(username="superadmin", email="s@x.com", password="superadmin@123", role=Role.SUPER_ADMIN)
        r = run_checks()
        self.assertEqual(level_of(r, "Demo accounts with their known passwords"), "FAIL")
        self.assertIn("superadmin", next(w for lv, w, _ in r if w.startswith("Demo accounts")))
        u = User.objects.get(username="superadmin")
        u.set_password("a-Really-Strong-one-9")
        u.save()
        self.assertEqual(level_of(run_checks(), "No demo account"), "OK")

    def test_administrators(self):
        self.assertEqual(level_of(run_checks(), "There is no active Super Admin"), "FAIL")
        User.objects.create_user(username="boss", email="b@x.com", password="Str0ng!pass99", role=Role.SUPER_ADMIN)
        self.assertEqual(level_of(run_checks(), "Only one administrator"), "WARN")
        User.objects.create_user(username="second", email="c@x.com", password="Str0ng!pass99", role=Role.ADMIN)
        self.assertEqual(level_of(run_checks(), "More than one administrator"), "OK")

    @override_settings(EMAIL_ENABLED=False)
    def test_missing_email_is_a_warning_with_advice(self):
        row = next(r for r in run_checks() if r[1] == "Email is not set up")
        self.assertEqual(row[0], "WARN")
        self.assertIn("support phone", row[2])

    def test_the_command_prints_a_summary_and_fails_when_something_must_be_fixed(self):
        out = StringIO()
        with self.assertRaises(CommandError):
            call_command("check_production", stdout=out)
        text = out.getvalue()
        self.assertIn("FAIL", text)
        self.assertIn("to fix.", text)
