from rest_framework.test import APITestCase

from .models import PlatformSettings, Role, User

PW = "Str0ng!pass99"


class SupportContactTests(APITestCase):
    def setUp(self):
        self.boss = User.objects.create_user(username="boss", email="b@x.com", password=PW, role=Role.SUPER_ADMIN)
        self.student = User.objects.create_user(username="stu", email="s@x.com", password=PW, role=Role.STUDENT)

    def test_the_support_contact_is_public_so_the_login_page_can_show_it(self):
        s = PlatformSettings.load()
        s.support_phone, s.support_hours = "+91 98765 43210", "Mon to Sat, 10 am to 6 pm"
        s.save()
        data = self.client.get("/api/platform/").data  # no sign-in
        self.assertEqual(data["support_phone"], "+91 98765 43210")
        self.assertEqual(data["support_hours"], "Mon to Sat, 10 am to 6 pm")

    def test_the_super_admin_sets_it_and_it_is_trimmed(self):
        self.client.force_authenticate(self.boss)
        r = self.client.patch("/api/platform/", {"support_phone": "  98765 43210 ", "support_hours": " 10 to 6 "}, format="json")
        self.assertEqual(r.status_code, 200, r.content)
        self.assertEqual((r.data["support_phone"], r.data["support_hours"]), ("98765 43210", "10 to 6"))
        self.assertEqual(PlatformSettings.load().support_phone, "98765 43210")

    def test_nobody_else_can_change_it(self):
        self.client.force_authenticate(self.student)
        self.assertEqual(self.client.patch("/api/platform/", {"support_phone": "1"}, format="json").status_code, 403)

    def test_it_is_empty_until_set(self):
        data = self.client.get("/api/platform/").data
        self.assertEqual((data["support_phone"], data["support_hours"]), ("", ""))
