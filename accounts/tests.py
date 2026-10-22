from rest_framework.test import APITestCase

from .models import ActivityLog, Role, User

PW = "Str0ng!pass99"


def make(username, role, password=PW):
    return User.objects.create_user(username=username, email=f"{username}@x.com", password=password, role=role)


class AuthTests(APITestCase):
    def test_register_creates_student_even_if_role_supplied(self):
        r = self.client.post("/api/auth/register/", {
            "username": "ann", "email": "ann@x.com", "password": PW,
            "first_name": "Ann", "role": "super_admin",
        })
        self.assertEqual(r.status_code, 201)
        self.assertEqual(User.objects.get(username="ann").role, Role.STUDENT)

    def test_register_rejects_weak_password_and_duplicate_email(self):
        r = self.client.post("/api/auth/register/", {"username": "a", "email": "a@x.com", "password": "123", "first_name": "A"})
        self.assertEqual(r.status_code, 400)
        make("bob", Role.STUDENT)
        r = self.client.post("/api/auth/register/", {"username": "b2", "email": "BOB@x.com", "password": PW, "first_name": "B"})
        self.assertEqual(r.status_code, 400)

    def test_login_by_username_and_email(self):
        make("cat", Role.STUDENT)
        for ident in ("cat", "cat@x.com"):
            r = self.client.post("/api/auth/login/", {"username": ident, "password": PW})
            self.assertEqual(r.status_code, 200, r.content)
            self.assertEqual(r.data["user"]["role"], Role.STUDENT)
            self.assertIn("access", r.data)

    def test_deactivated_user_cannot_login(self):
        u = make("eve", Role.STUDENT)
        u.is_active = False
        u.save()
        r = self.client.post("/api/auth/login/", {"username": "eve", "password": PW})
        self.assertEqual(r.status_code, 401)

    def test_me_requires_auth_and_blocks_role_edit(self):
        self.assertEqual(self.client.get("/api/auth/me/").status_code, 401)
        u = make("dan", Role.STUDENT)
        self.client.force_authenticate(u)
        r = self.client.patch("/api/auth/me/", {"role": "super_admin", "first_name": "Dan"})
        self.assertEqual(r.status_code, 200)
        u.refresh_from_db()
        self.assertEqual(u.role, Role.STUDENT)
        self.assertEqual(u.first_name, "Dan")


class UserManagementTests(APITestCase):
    def setUp(self):
        self.sa = make("sa", Role.SUPER_ADMIN)
        self.admin = make("adm", Role.ADMIN)
        self.stu = make("stu", Role.STUDENT)

    def test_student_cannot_access_user_api(self):
        self.client.force_authenticate(self.stu)
        self.assertEqual(self.client.get("/api/users/").status_code, 403)

    def test_admin_sees_only_students_and_cannot_create_admin(self):
        self.client.force_authenticate(self.admin)
        r = self.client.get("/api/users/")
        self.assertEqual({u["role"] for u in r.data["results"]}, {Role.STUDENT})
        body = {"username": "n", "email": "n@x.com", "password": PW}
        self.assertEqual(self.client.post("/api/users/", {**body, "role": "admin"}).status_code, 403)
        self.assertEqual(self.client.post("/api/users/", {**body, "role": "student"}).status_code, 201)
        self.assertEqual(self.client.get(f"/api/users/{self.sa.pk}/").status_code, 404)

    def test_super_admin_creates_admin_and_logs_it(self):
        self.client.force_authenticate(self.sa)
        r = self.client.post("/api/users/", {"username": "n", "email": "n@x.com", "password": PW, "role": "admin"})
        self.assertEqual(r.status_code, 201)
        self.assertTrue(ActivityLog.objects.filter(action="user.create").exists())
        self.assertEqual(self.client.get("/api/activity-logs/").status_code, 200)

    def test_admin_cannot_view_activity_logs(self):
        self.client.force_authenticate(self.admin)
        self.assertEqual(self.client.get("/api/activity-logs/").status_code, 403)

    def test_delete_is_soft_and_self_protected(self):
        self.client.force_authenticate(self.admin)
        self.assertEqual(self.client.delete(f"/api/users/{self.stu.pk}/").status_code, 204)
        self.stu.refresh_from_db()
        self.assertFalse(self.stu.is_active)
        self.client.force_authenticate(self.sa)
        self.assertEqual(self.client.delete(f"/api/users/{self.sa.pk}/").status_code, 403)


class TokenHardeningTests(APITestCase):
    def login(self, name="tok"):
        make(name, Role.STUDENT)
        return self.client.post("/api/auth/login/", {"username": name, "password": PW}).data

    def test_refresh_rotates_and_old_refresh_token_is_dead(self):
        tokens = self.login()
        r = self.client.post("/api/auth/refresh/", {"refresh": tokens["refresh"]})
        self.assertEqual(r.status_code, 200)
        self.assertIn("refresh", r.data)
        self.assertNotEqual(r.data["refresh"], tokens["refresh"])
        # Replaying the retired token fails.
        self.assertEqual(self.client.post("/api/auth/refresh/", {"refresh": tokens["refresh"]}).status_code, 401)
        self.assertEqual(self.client.post("/api/auth/refresh/", {"refresh": r.data["refresh"]}).status_code, 200)

    def test_logout_blacklists_refresh_token(self):
        tokens = self.login()
        self.assertEqual(self.client.post("/api/auth/logout/", {"refresh": tokens["refresh"]}).status_code, 204)
        self.assertEqual(self.client.post("/api/auth/refresh/", {"refresh": tokens["refresh"]}).status_code, 401)
        # Garbage or repeated logouts never error.
        self.assertEqual(self.client.post("/api/auth/logout/", {"refresh": "junk"}).status_code, 204)
        self.assertEqual(self.client.post("/api/auth/logout/", {}).status_code, 204)

    def test_deactivated_user_loses_access_immediately(self):
        tokens = self.login()
        u = User.objects.get(username="tok")
        u.is_active = False
        u.save()
        self.assertEqual(self.client.get("/api/auth/me/", HTTP_AUTHORIZATION=f"Bearer {tokens['access']}").status_code, 401)
        self.assertEqual(self.client.post("/api/auth/refresh/", {"refresh": tokens["refresh"]}).status_code, 401)

    def test_tampered_token_rejected(self):
        tokens = self.login()
        bad = tokens["access"][:-3] + "abc"
        self.assertEqual(self.client.get("/api/auth/me/", HTTP_AUTHORIZATION=f"Bearer {bad}").status_code, 401)


class PlatformSettingsTests(APITestCase):
    def test_public_read_has_defaults(self):
        r = self.client.get("/api/platform/")
        self.assertEqual(r.status_code, 200)
        self.assertEqual((r.data["platform_name"], r.data["allow_registration"]), ("TestPrep", True))

    def test_public_read_survives_a_bad_token(self):
        self.assertEqual(self.client.get("/api/platform/", HTTP_AUTHORIZATION="Bearer garbage").status_code, 200)

    def test_only_super_admin_can_change_settings(self):
        for who, expected in (("admin", 403), ("student", 403)):
            self.client.force_authenticate(make(who, Role.ADMIN if who == "admin" else Role.STUDENT))
            self.assertEqual(self.client.patch("/api/platform/", {"platform_name": "X"}, format="json").status_code, expected)
        self.client.force_authenticate(None)
        self.assertEqual(self.client.patch("/api/platform/", {"platform_name": "X"}, format="json").status_code, 401)
        self.client.force_authenticate(make("boss", Role.SUPER_ADMIN))
        r = self.client.patch("/api/platform/", {"platform_name": " Acme Exams ", "support_email": "help@acme.test"}, format="json")
        self.assertEqual(r.status_code, 200, r.content)
        self.assertEqual(self.client.get("/api/platform/").data["platform_name"], "Acme Exams")
        self.assertTrue(ActivityLog.objects.filter(action="platform.update").exists())
        self.assertEqual(self.client.patch("/api/platform/", {"platform_name": "  "}, format="json").status_code, 400)
        self.assertEqual(self.client.patch("/api/platform/", {"support_email": "nope"}, format="json").status_code, 400)

    def test_closing_registration_blocks_sign_up_but_not_admin_created_accounts(self):
        self.client.force_authenticate(make("boss", Role.SUPER_ADMIN))
        self.client.patch("/api/platform/", {"allow_registration": False}, format="json")
        self.client.force_authenticate(None)
        r = self.client.post("/api/auth/register/", {"username": "new", "email": "n@x.com", "password": PW, "first_name": "N"})
        self.assertEqual(r.status_code, 403)
        self.assertFalse(User.objects.filter(username="new").exists())
        self.client.force_authenticate(User.objects.get(username="boss"))
        r = self.client.post("/api/users/", {"username": "made", "email": "m@x.com", "password": PW, "role": "student"})
        self.assertEqual(r.status_code, 201)
        # And it can be reopened.
        self.client.patch("/api/platform/", {"allow_registration": True}, format="json")
        self.client.force_authenticate(None)
        r = self.client.post("/api/auth/register/", {"username": "new", "email": "n@x.com", "password": PW, "first_name": "N"})
        self.assertEqual(r.status_code, 201)
