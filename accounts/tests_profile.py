from rest_framework.test import APITestCase

from .models import Role, User

PW = "Str0ng!pass99"


class ProfileTests(APITestCase):
    def setUp(self):
        self.user = User.objects.create_user(
            username="dan", email="dan@x.com", password=PW, role=Role.STUDENT, first_name="Dan", last_name="Old", phone="9999999999"
        )
        self.client.force_authenticate(self.user)

    def patch(self, **body):
        return self.client.patch("/api/auth/me/", body, format="json")

    def test_changes_name_and_phone(self):
        r = self.patch(first_name="  Daniel ", last_name="New  Name", phone="+91 98765-43210")
        self.assertEqual(r.status_code, 200, r.content)
        self.user.refresh_from_db()
        self.assertEqual((self.user.first_name, self.user.last_name, self.user.phone), ("Daniel", "New Name", "+91 98765-43210"))
        self.assertEqual(r.data["first_name"], "Daniel")

    def test_a_single_field_leaves_the_others_alone(self):
        self.assertEqual(self.patch(last_name="Fresh").status_code, 200)
        self.user.refresh_from_db()
        self.assertEqual((self.user.first_name, self.user.last_name, self.user.phone), ("Dan", "Fresh", "9999999999"))

    def test_first_name_is_required_but_phone_can_be_cleared(self):
        self.assertEqual(self.patch(first_name="   ").status_code, 400)
        self.assertEqual(self.patch(phone="").status_code, 200)
        self.user.refresh_from_db()
        self.assertEqual(self.user.phone, "")

    def test_bad_phone_numbers_are_refused(self):
        for bad in ("abc", "12", "<script>", "99999 99999 99999 99999 999"):
            self.assertEqual(self.patch(phone=bad).status_code, 400, bad)

    def test_username_email_and_role_cannot_be_changed_here(self):
        r = self.patch(username="hacker", email="h@x.com", role="super_admin", is_active=False, first_name="Dan")
        self.assertEqual(r.status_code, 200)
        self.user.refresh_from_db()
        self.assertEqual((self.user.username, self.user.email, self.user.role, self.user.is_active), ("dan", "dan@x.com", Role.STUDENT, True))

    def test_new_password_must_differ_from_the_old_one(self):
        r = self.client.post("/api/auth/change-password/", {"old_password": PW, "new_password": PW}, format="json")
        self.assertEqual(r.status_code, 400)
        self.assertIn("different", str(r.data))
        r = self.client.post("/api/auth/change-password/", {"old_password": PW, "new_password": "Br4nd!new-pass"}, format="json")
        self.assertEqual(r.status_code, 200, r.content)
        self.user.refresh_from_db()
        self.assertTrue(self.user.check_password("Br4nd!new-pass"))

    def test_wrong_current_password(self):
        r = self.client.post("/api/auth/change-password/", {"old_password": "nope", "new_password": "Br4nd!new-pass"}, format="json")
        self.assertEqual(r.status_code, 400)


class PreferredLanguageTests(ProfileTests):
    """The reading language is kept on the account."""

    def test_language_is_saved_and_returned_on_every_login(self):
        self.assertEqual(self.patch(preferred_language="hi").data["preferred_language"], "hi")
        self.client.force_authenticate(None)
        r = self.client.post("/api/auth/login/", {"username": "dan", "password": PW}, format="json")
        self.assertEqual(r.status_code, 200, r.content)
        self.assertEqual(r.data["user"]["preferred_language"], "hi")

    def test_only_english_or_hindi_or_nothing(self):
        self.assertEqual(self.patch(preferred_language="fr").status_code, 400)
        self.assertEqual(self.patch(preferred_language="").status_code, 200)
