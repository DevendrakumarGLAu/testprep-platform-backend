import hashlib
import hmac
from datetime import timedelta

from django.conf import settings
from django.core.cache import cache
from django.test import override_settings
from django.utils import timezone
from rest_framework.test import APITestCase

from . import captcha
from .models import CaptchaChallenge, Role, User

PW = "Str0ng!pass99"


def solve(token, code):
    """Test helper: the answer is only known to the server, so write the code the way the server stored it."""
    CaptchaChallenge.objects.filter(token=token).update(answer_hash=hmac.new(settings.SECRET_KEY.encode(), code.encode(), hashlib.sha256).hexdigest())
    return {"captcha_token": token, "captcha_answer": code}


@override_settings(CAPTCHA_ENABLED=True)
class CaptchaTests(APITestCase):
    def setUp(self):
        cache.clear()  # the login rate limit is counted per minute; start every test with a clean count
        self.addCleanup(cache.clear)
        self.user = User.objects.create_user(username="stu", email="s@x.com", password=PW, role=Role.STUDENT)

    def fresh(self, code="AB3CD"):
        data = self.client.get("/api/auth/captcha/").data
        return solve(data["token"], code)

    def login(self, extra=None, password=PW):
        return self.client.post("/api/auth/login/", {"username": "stu", "password": password, **(extra or {})}, format="json")

    def test_captcha_endpoint_returns_a_png_and_a_token(self):
        data = self.client.get("/api/auth/captcha/").data
        self.assertTrue(data["enabled"])
        self.assertTrue(data["image"].startswith("data:image/png;base64,"))
        self.assertEqual(len(data["token"]), 32)
        self.assertEqual(CaptchaChallenge.objects.count(), 1)
        # only a hash of the answer is stored
        self.assertEqual(len(CaptchaChallenge.objects.get().answer_hash), 64)

    def test_login_needs_the_captcha(self):
        r = self.login()
        self.assertEqual(r.status_code, 400)
        self.assertIn("characters", r.data["detail"])

    def test_login_with_the_right_answer(self):
        self.assertEqual(self.login(self.fresh()).status_code, 200)

    def test_answer_is_not_case_sensitive(self):
        data = self.client.get("/api/auth/captcha/").data
        extra = solve(data["token"], "AB3CD")
        extra["captcha_answer"] = " ab3cd "
        self.assertEqual(self.login(extra).status_code, 200)

    def test_wrong_answer_is_refused_and_the_picture_is_used_up(self):
        data = self.client.get("/api/auth/captcha/").data
        solve(data["token"], "AB3CD")
        bad = {"captcha_token": data["token"], "captcha_answer": "ZZZZZ"}
        self.assertEqual(self.login(bad).status_code, 400)
        self.assertFalse(CaptchaChallenge.objects.filter(token=data["token"]).exists())
        # the right answer on the same picture no longer works: one guess per picture
        self.assertEqual(self.login({"captcha_token": data["token"], "captcha_answer": "AB3CD"}).status_code, 400)

    def test_a_solved_picture_cannot_be_replayed(self):
        extra = self.fresh()
        self.assertEqual(self.login(extra).status_code, 200)
        self.assertEqual(self.login(extra).status_code, 400)

    def test_expired_picture(self):
        extra = self.fresh()
        CaptchaChallenge.objects.update(created_at=timezone.now() - timedelta(minutes=6))
        r = self.login(extra)
        self.assertEqual(r.status_code, 400)
        self.assertIn("expired", r.data["detail"])

    def test_unknown_token(self):
        r = self.login({"captcha_token": "nope", "captcha_answer": "ABCDE"})
        self.assertEqual(r.status_code, 400)

    def test_a_good_captcha_does_not_make_a_wrong_password_work(self):
        self.assertEqual(self.login(self.fresh(), password="wrong-password").status_code, 401)

    def test_registration_needs_it(self):
        body = {"username": "new", "email": "n@x.com", "password": PW, "first_name": "N"}
        self.assertEqual(self.client.post("/api/auth/register/", body, format="json").status_code, 400)
        r = self.client.post("/api/auth/register/", {**body, **self.fresh()}, format="json")
        self.assertEqual(r.status_code, 201, r.content)

    def test_change_password_needs_it(self):
        self.client.force_authenticate(self.user)
        body = {"old_password": PW, "new_password": "An0ther!pass88"}
        self.assertEqual(self.client.post("/api/auth/change-password/", body, format="json").status_code, 400)
        r = self.client.post("/api/auth/change-password/", {**body, **self.fresh()}, format="json")
        self.assertEqual(r.status_code, 200, r.content)

    def test_old_challenges_are_cleaned_up(self):
        CaptchaChallenge.objects.create(token="old", answer_hash="x")
        CaptchaChallenge.objects.update(created_at=timezone.now() - timedelta(hours=1))
        captcha.new_challenge()
        self.assertFalse(CaptchaChallenge.objects.filter(token="old").exists())

    @override_settings(CAPTCHA_ENABLED=False)
    def test_switched_off(self):
        self.assertEqual(self.client.get("/api/auth/captcha/").data, {"enabled": False})
        self.assertEqual(self.login().status_code, 200)
