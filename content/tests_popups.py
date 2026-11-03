from datetime import timedelta

from django.core.files.storage import default_storage
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import override_settings
from django.utils import timezone
from rest_framework.test import APITestCase

from accounts.models import Role, User
from core.tests_media import CLOUD, picture
from exams.models import Batch, ExamCategory, Preparation

from .models import Popup, PopupSeen

PW = "Str0ng!pass99"


class PopupTests(APITestCase):
    def setUp(self):
        self.admin = User.objects.create_user(username="adm", email="a@x.com", password=PW, role=Role.ADMIN)
        self.stu = User.objects.create_user(username="stu", email="s@x.com", password=PW, role=Role.STUDENT)
        self.other = User.objects.create_user(username="oth", email="o@x.com", password=PW, role=Role.STUDENT)
        self.cat = ExamCategory.objects.create(name="SSC")
        self.batch = Batch.objects.create(name="Morning")

    def make(self, **over):
        body = {"title": "New: study plan", "body": "Plan your week.", "audience": "all", **over}
        self.client.force_authenticate(self.admin)
        r = self.client.post("/api/popups/", body, format="json")
        self.assertEqual(r.status_code, 201, r.content)
        return r.data

    def pending(self, user):
        self.client.force_authenticate(user)
        r = self.client.get("/api/student/popups/pending/")
        self.assertEqual(r.status_code, 200)
        return r.data["popup"]

    def test_admin_creates_a_popup_and_students_cannot(self):
        self.make()
        self.client.force_authenticate(self.stu)
        self.assertEqual(self.client.post("/api/popups/", {"title": "x"}, format="json").status_code, 403)
        self.assertEqual(self.client.get("/api/popups/").status_code, 403)

    def test_a_student_sees_it_once_after_closing(self):
        made = self.make()
        self.assertEqual(self.pending(self.stu)["id"], made["id"])
        self.assertEqual(self.client.post(f"/api/student/popups/{made['id']}/seen/").status_code, 204)
        self.assertIsNone(self.pending(self.stu))
        self.assertIsNotNone(self.pending(self.other))
        self.client.force_authenticate(self.stu)
        self.client.post(f"/api/student/popups/{made['id']}/seen/")  # twice is harmless
        self.assertEqual(PopupSeen.objects.count(), 1)

    def test_the_newest_comes_first_then_the_next(self):
        old = self.make(title="Old")
        new = self.make(title="New")
        self.assertEqual(self.pending(self.stu)["id"], new["id"])
        self.client.post(f"/api/student/popups/{new['id']}/seen/")
        self.assertEqual(self.pending(self.stu)["id"], old["id"])

    def test_off_scheduled_and_ended_popups_are_not_shown(self):
        now = timezone.now()
        off = self.make(is_active=False)
        self.make(title="Later", starts_at=(now + timedelta(days=1)).isoformat())
        self.make(title="Gone", starts_at=(now - timedelta(days=3)).isoformat(), ends_at=(now - timedelta(days=1)).isoformat())
        self.assertIsNone(self.pending(self.stu))
        self.client.force_authenticate(self.admin)
        data = self.client.get("/api/popups/").data
        rows = data["results"] if isinstance(data, dict) else data
        self.assertEqual(sorted(p["state"] for p in rows), ["ended", "off", "scheduled"])
        Popup.objects.filter(pk=off["id"]).update(is_active=True)
        self.assertEqual(self.pending(self.stu)["id"], off["id"])

    def test_an_exam_popup_goes_only_to_students_preparing_for_it(self):
        self.make(audience="exam", category=self.cat.id)
        Preparation.objects.create(student=self.stu, category=self.cat)
        self.assertIsNotNone(self.pending(self.stu))
        self.assertIsNone(self.pending(self.other))

    def test_a_batch_popup_goes_only_to_that_batch(self):
        self.make(audience="batch", batch=self.batch.id)
        self.batch.students.add(self.stu)
        self.assertIsNotNone(self.pending(self.stu))
        self.assertIsNone(self.pending(self.other))

    def test_the_target_is_required_and_the_other_one_is_dropped(self):
        self.client.force_authenticate(self.admin)
        self.assertEqual(self.client.post("/api/popups/", {"title": "x", "audience": "exam"}, format="json").status_code, 400)
        self.assertEqual(self.client.post("/api/popups/", {"title": "x", "audience": "batch"}, format="json").status_code, 400)
        made = self.make(audience="all", category=self.cat.id, batch=self.batch.id)
        self.assertIsNone(made["category"])
        self.assertIsNone(made["batch"])

    def test_the_link_must_be_an_app_page_or_https(self):
        self.client.force_authenticate(self.admin)
        for bad in ["http://x.com", "//evil.com", "javascript:alert(1)", "student/plan"]:
            r = self.client.post("/api/popups/", {"title": "x", "link": bad}, format="json")
            self.assertEqual(r.status_code, 400, bad)
        made = self.make(link="/student/plan", button_label="")
        self.assertEqual(made["button_label"], "Open")
        self.make(link="https://example.com/launch")

    def test_the_end_must_be_after_the_start_and_a_title_is_needed(self):
        self.client.force_authenticate(self.admin)
        now = timezone.now()
        r = self.client.post("/api/popups/", {"title": "x", "starts_at": now.isoformat(), "ends_at": (now - timedelta(hours=1)).isoformat()}, format="json")
        self.assertEqual(r.status_code, 400)
        self.assertEqual(self.client.post("/api/popups/", {"title": "  "}, format="json").status_code, 400)

    def test_the_admin_list_shows_state_seen_and_audience_size(self):
        made = self.make()
        self.pending(self.stu)
        self.client.post(f"/api/student/popups/{made['id']}/seen/")
        self.client.force_authenticate(self.admin)
        data = self.client.get("/api/popups/").data
        rows = data["results"] if isinstance(data, dict) else data
        self.assertEqual(rows[0]["seen_count"], 1)
        self.assertEqual(rows[0]["audience_size"], 2)
        self.assertEqual(rows[0]["state"], "live")

    def test_admin_can_edit_and_delete(self):
        made = self.make()
        self.client.force_authenticate(self.admin)
        r = self.client.patch(f"/api/popups/{made['id']}/", {"is_active": False}, format="json")
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.data["state"], "off")
        self.assertEqual(self.client.delete(f"/api/popups/{made['id']}/").status_code, 204)
        self.assertEqual(Popup.objects.count(), 0)

    def test_only_students_use_the_student_endpoints(self):
        self.client.force_authenticate(self.admin)
        self.assertEqual(self.client.get("/api/student/popups/pending/").status_code, 403)


@override_settings(STORAGES=CLOUD)
class PopupImageTests(APITestCase):
    def setUp(self):
        self.admin = User.objects.create_user(username="adm", email="a@x.com", password=PW, role=Role.ADMIN)
        self.stu = User.objects.create_user(username="stu", email="s@x.com", password=PW, role=Role.STUDENT)
        self.popup = Popup.objects.create(title="Launch", created_by=self.admin)

    def upload(self, f, user=None):
        self.client.force_authenticate(user or self.admin)
        return self.client.post(f"/api/popups/{self.popup.id}/image/", {"image": f}, format="multipart")

    def test_admin_uploads_a_picture_and_students_get_its_address(self):
        r = self.upload(picture())
        self.assertEqual(r.status_code, 200, r.content)
        self.assertRegex(r.data["image_url"], r"^https://cdn\.example/storage/popups/[0-9a-f]+\.png$")
        self.client.force_authenticate(self.stu)
        self.assertEqual(self.client.get("/api/student/popups/pending/").data["popup"]["image_url"], r.data["image_url"])

    def test_a_popup_without_a_picture_has_no_address(self):
        self.client.force_authenticate(self.stu)
        self.assertIsNone(self.client.get("/api/student/popups/pending/").data["popup"]["image_url"])

    def test_a_new_picture_replaces_the_old_file(self):
        self.upload(picture(name="one"))
        first = Popup.objects.get(pk=self.popup.id).image.name
        self.upload(picture(name="two"))
        second = Popup.objects.get(pk=self.popup.id).image.name
        self.assertNotEqual(first, second)
        self.assertFalse(default_storage.exists(first))
        self.assertTrue(default_storage.exists(second))

    def test_removing_and_deleting_clean_up_the_file(self):
        self.upload(picture())
        name = Popup.objects.get(pk=self.popup.id).image.name
        self.client.force_authenticate(self.admin)
        r = self.client.delete(f"/api/popups/{self.popup.id}/image/")
        self.assertIsNone(r.data["image_url"])
        self.assertFalse(default_storage.exists(name))
        self.upload(picture())
        name = Popup.objects.get(pk=self.popup.id).image.name
        self.client.delete(f"/api/popups/{self.popup.id}/")
        self.assertFalse(default_storage.exists(name))

    def test_bad_uploads_are_refused(self):
        self.assertEqual(self.upload(SimpleUploadedFile("a.png", b"not a picture", content_type="image/png")).status_code, 400)
        self.assertEqual(self.upload(SimpleUploadedFile("a.txt", b"hello", content_type="text/plain")).status_code, 400)
        self.assertEqual(self.upload(SimpleUploadedFile("big.png", b"x" * (2 * 1024 * 1024 + 1), content_type="image/png")).status_code, 400)
        self.client.force_authenticate(self.admin)
        self.assertEqual(self.client.post(f"/api/popups/{self.popup.id}/image/", {}, format="multipart").status_code, 400)

    def test_students_cannot_upload(self):
        self.assertEqual(self.upload(picture(), self.stu).status_code, 403)
