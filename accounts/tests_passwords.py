import re

from django.core import mail
from django.core.cache import cache
from django.test import override_settings
from rest_framework.test import APITestCase

from . import emails
from .models import Role, User

PW = "Str0ng!pass99"
EMAIL_ON = dict(EMAIL_ENABLED=True, EMAIL_BACKEND="django.core.mail.backends.locmem.EmailBackend", FRONTEND_URL="https://app.example.com")


class Base(APITestCase):
    def setUp(self):
        cache.clear()
        self.addCleanup(cache.clear)
        self.dan = User.objects.create_user(username="dan", email="dan@x.com", password=PW, role=Role.STUDENT, first_name="Dan")
        self.admin = User.objects.create_user(username="adm", email="a@x.com", password=PW, role=Role.ADMIN)
        self.boss = User.objects.create_user(username="boss", email="b@x.com", password=PW, role=Role.SUPER_ADMIN)


@override_settings(**EMAIL_ON)
class ForgotAndResetTests(Base):
    def forgot(self, email="dan@x.com"):
        return self.client.post("/api/auth/forgot-password/", {"email": email}, format="json")

    def link_parts(self):
        body = mail.outbox[-1].body
        m = re.search(r"https://app\.example\.com/reset-password/([^/\s]+)/([^/\s]+)", body)
        self.assertIsNotNone(m, body)
        return m.group(1), m.group(2)

    def test_a_link_is_emailed_to_the_account(self):
        r = self.forgot()
        self.assertEqual(r.status_code, 200)
        self.assertEqual(len(mail.outbox), 1)
        self.assertEqual(mail.outbox[0].to, ["dan@x.com"])
        self.assertIn("reset", mail.outbox[0].subject.lower())

    def test_the_reply_is_the_same_for_an_unknown_address(self):
        known, unknown = self.forgot(), self.forgot("nobody@x.com")
        self.assertEqual((known.status_code, known.data), (unknown.status_code, unknown.data))
        self.assertEqual(len(mail.outbox), 1)

    def test_inactive_accounts_get_nothing(self):
        User.objects.filter(pk=self.dan.pk).update(is_active=False)
        self.forgot()
        self.assertEqual(len(mail.outbox), 0)

    def test_an_address_is_required(self):
        self.assertEqual(self.client.post("/api/auth/forgot-password/", {}, format="json").status_code, 400)

    def test_the_link_sets_a_new_password_and_works_once(self):
        self.forgot()
        uid, token = self.link_parts()
        r = self.client.post("/api/auth/reset-password/", {"uid": uid, "token": token, "new_password": "Br4nd!new-pass"}, format="json")
        self.assertEqual(r.status_code, 200, r.content)
        self.dan.refresh_from_db()
        self.assertTrue(self.dan.check_password("Br4nd!new-pass"))
        again = self.client.post("/api/auth/reset-password/", {"uid": uid, "token": token, "new_password": "An0ther!pass88"}, format="json")
        self.assertEqual(again.status_code, 400)
        self.assertTrue(User.objects.get(pk=self.dan.pk).check_password("Br4nd!new-pass"))

    def test_weak_passwords_and_bad_links_are_refused(self):
        self.forgot()
        uid, token = self.link_parts()
        weak = self.client.post("/api/auth/reset-password/", {"uid": uid, "token": token, "new_password": "123"}, format="json")
        self.assertEqual(weak.status_code, 400)
        self.assertIn("new_password", weak.data)
        for body in ({"uid": uid, "token": "wrong", "new_password": "Br4nd!new-pass"}, {"uid": "zz", "token": token, "new_password": "Br4nd!new-pass"}, {}):
            self.assertEqual(self.client.post("/api/auth/reset-password/", body, format="json").status_code, 400)

    def test_resetting_clears_a_forced_change_and_signs_other_devices_out(self):
        login = self.client.post("/api/auth/login/", {"username": "dan", "password": PW}, format="json")
        refresh = login.data["refresh"]
        User.objects.filter(pk=self.dan.pk).update(must_change_password=True)
        self.forgot()
        uid, token = self.link_parts()
        self.client.post("/api/auth/reset-password/", {"uid": uid, "token": token, "new_password": "Br4nd!new-pass"}, format="json")
        self.assertFalse(User.objects.get(pk=self.dan.pk).must_change_password)
        self.assertEqual(self.client.post("/api/auth/refresh/", {"refresh": refresh}, format="json").status_code, 401)

    @override_settings(EMAIL_ENABLED=False)
    def test_without_an_email_service_it_says_to_ask_an_administrator(self):
        r = self.forgot()
        self.assertEqual(r.status_code, 503)
        self.assertIn("administrator", r.data["detail"])

    def test_the_public_settings_say_whether_emails_work(self):
        self.assertTrue(self.client.get("/api/platform/").data["email_enabled"])
        with override_settings(EMAIL_ENABLED=False):
            self.assertFalse(self.client.get("/api/platform/").data["email_enabled"])


@override_settings(**EMAIL_ON)
class VerificationTests(Base):
    def token(self):
        return re.search(r"/verify-email/(\S+)", mail.outbox[-1].body).group(1)

    def test_a_signed_in_person_can_ask_for_a_link_and_the_link_confirms_the_address(self):
        self.client.force_authenticate(self.dan)
        self.assertEqual(self.client.post("/api/auth/send-verification/").status_code, 200)
        self.assertEqual(mail.outbox[0].to, ["dan@x.com"])
        self.client.force_authenticate(None)  # the link works from any device
        r = self.client.post("/api/auth/verify-email/", {"token": self.token()}, format="json")
        self.assertEqual(r.status_code, 200, r.content)
        self.assertTrue(User.objects.get(pk=self.dan.pk).email_verified)

    def test_already_confirmed_people_are_not_emailed_again(self):
        User.objects.filter(pk=self.dan.pk).update(email_verified=True)
        self.dan.refresh_from_db()
        self.client.force_authenticate(self.dan)
        r = self.client.post("/api/auth/send-verification/")
        self.assertTrue(r.data["already"])
        self.assertEqual(len(mail.outbox), 0)

    def test_a_link_for_an_old_address_stops_working_and_a_new_address_needs_confirming(self):
        self.client.force_authenticate(self.dan)
        self.client.post("/api/auth/send-verification/")
        token = self.token()
        User.objects.filter(pk=self.dan.pk).update(email_verified=True)
        dan = User.objects.get(pk=self.dan.pk)
        dan.email = "new@x.com"
        dan.save()
        dan.refresh_from_db()
        self.assertFalse(dan.email_verified)
        self.assertEqual(self.client.post("/api/auth/verify-email/", {"token": token}, format="json").status_code, 400)

    def test_bad_or_expired_links(self):
        self.assertEqual(self.client.post("/api/auth/verify-email/", {"token": "nonsense"}, format="json").status_code, 400)
        self.assertEqual(self.client.post("/api/auth/verify-email/", {}, format="json").status_code, 400)
        from django.core import signing
        old = signing.dumps({"uid": self.dan.pk, "email": self.dan.email}, salt=emails.VERIFY_SALT)
        with self.settings():
            import time
            from unittest import mock
            with mock.patch("django.core.signing.time.time", return_value=time.time() + emails.VERIFY_MAX_AGE + 60):
                self.assertEqual(self.client.post("/api/auth/verify-email/", {"token": old}, format="json").status_code, 400)

    def test_signing_up_sends_a_confirmation_link(self):
        r = self.client.post("/api/auth/register/", {"username": "newbie", "email": "n@x.com", "password": PW, "first_name": "N"}, format="json")
        self.assertEqual(r.status_code, 201, r.content)
        self.assertEqual(mail.outbox[0].to, ["n@x.com"])
        self.assertFalse(r.data["email_verified"])

    @override_settings(EMAIL_ENABLED=False)
    def test_without_an_email_service_sign_up_still_works(self):
        r = self.client.post("/api/auth/register/", {"username": "newbie", "email": "n@x.com", "password": PW, "first_name": "N"}, format="json")
        self.assertEqual(r.status_code, 201)
        self.assertEqual(len(mail.outbox), 0)

    def test_needs_sign_in_to_ask_for_a_link(self):
        self.assertEqual(self.client.post("/api/auth/send-verification/").status_code, 401)


class AdminResetTests(Base):
    def reset(self, who, target):
        self.client.force_authenticate(who)
        return self.client.post(f"/api/users/{target.pk}/reset-password/")

    def test_admin_gives_a_student_a_temporary_password_that_must_be_changed(self):
        r = self.reset(self.admin, self.dan)
        self.assertEqual(r.status_code, 200, r.content)
        temp = r.data["temporary_password"]
        self.assertEqual(len(temp), 10)
        self.dan.refresh_from_db()
        self.assertTrue(self.dan.check_password(temp))
        self.assertTrue(self.dan.must_change_password)
        self.assertFalse(self.dan.check_password(PW))

    def test_the_student_signs_in_with_it_and_is_told_to_change_it(self):
        temp = self.reset(self.admin, self.dan).data["temporary_password"]
        self.client.force_authenticate(None)
        login = self.client.post("/api/auth/login/", {"username": "dan", "password": temp}, format="json")
        self.assertEqual(login.status_code, 200)
        self.assertTrue(login.data["user"]["must_change_password"])
        self.dan.refresh_from_db()
        self.client.force_authenticate(self.dan)
        r = self.client.post("/api/auth/change-password/", {"old_password": temp, "new_password": "Br4nd!new-pass"}, format="json")
        self.assertEqual(r.status_code, 200, r.content)
        self.assertFalse(User.objects.get(pk=self.dan.pk).must_change_password)

    def test_their_other_sessions_end(self):
        refresh = self.client.post("/api/auth/login/", {"username": "dan", "password": PW}, format="json").data["refresh"]
        self.reset(self.admin, self.dan)
        self.assertEqual(self.client.post("/api/auth/refresh/", {"refresh": refresh}, format="json").status_code, 401)

    def test_an_admin_cannot_reset_another_admin_or_themselves_but_a_super_admin_can_reset_an_admin(self):
        self.assertEqual(self.reset(self.admin, self.boss).status_code, 404)  # admins only see students
        self.assertEqual(self.reset(self.admin, self.admin).status_code, 404)
        self.assertEqual(self.reset(self.boss, self.boss).status_code, 403)
        self.assertEqual(self.reset(self.boss, self.admin).status_code, 200)

    def test_students_cannot_reset_anyone(self):
        self.assertEqual(self.reset(self.dan, self.admin).status_code, 403)
