from decimal import Decimal

from rest_framework.test import APITestCase

from accounts.models import Role, User
from attempts.tests import EngineBase
from exams.models import Batch, Exam, ExamCategory

PW = "Str0ng!pass99"


class PremiumTests(EngineBase):
    def setUp(self):
        super().setUp()
        self.premium = self.make_exam(is_premium=True, price=Decimal("199.00"), max_attempts=0)

    def card(self, who, exam=None):
        self.client.force_authenticate(who)
        rows = self.client.get("/api/student/exams/").data
        return next(e for e in rows if e["id"] == (exam or self.premium).public_id)

    def grant(self, **body):
        self.client.force_authenticate(self.admin)
        r = self.client.put(f"/api/exams/{self.premium.id}/premium-access/", body, format="json")
        self.assertEqual(r.status_code, 200, r.content)
        return r.data

    def test_a_premium_test_is_listed_but_locked_for_everyone_without_access(self):
        c = self.card(self.stu)
        self.assertEqual((c["premium"], c["price"], c["locked"], c["status"], c["can_start"]), (True, "199.00", True, "locked", False))
        self.assertIn("premium", c["reason"].lower())

    def test_starting_a_locked_test_is_refused(self):
        self.client.force_authenticate(self.stu)
        r = self.client.post(f"/api/student/exams/{self.premium.public_id}/start/")
        self.assertEqual(r.status_code, 403)
        self.assertIn("premium", r.data["detail"].lower())
        self.assertEqual(self.premium.attempts.count(), 0)

    def test_granting_access_to_a_student_unlocks_it_for_them_only(self):
        data = self.grant(students=[self.stu.id])
        self.assertEqual(data["premium_student_ids"], [self.stu.id])
        c = self.card(self.stu)
        self.assertEqual((c["locked"], c["status"], c["can_start"]), (False, "available", True))
        self.assertTrue(self.card(self.other)["locked"])
        self.client.force_authenticate(self.stu)
        self.assertEqual(self.client.post(f"/api/student/exams/{self.premium.public_id}/start/").status_code, 201)

    def test_a_batch_can_be_given_access(self):
        batch = Batch.objects.create(name="Gold")
        batch.students.add(self.other)
        self.grant(batches=[batch.id])
        self.assertFalse(self.card(self.other)["locked"])
        self.assertTrue(self.card(self.stu)["locked"])

    def test_assigning_the_test_to_a_student_also_counts_as_access(self):
        Exam.objects.filter(pk=self.premium.pk).update(access_type=Exam.Access.ASSIGNED)
        self.premium.assigned_students.add(self.stu)
        self.assertFalse(self.card(self.stu)["locked"])

    def test_access_can_be_taken_away_but_a_running_attempt_can_be_finished(self):
        self.grant(students=[self.stu.id])
        self.client.force_authenticate(self.stu)
        a = self.client.post(f"/api/student/exams/{self.premium.public_id}/start/").data["attempt_id"]
        self.grant(students=[])
        c = self.card(self.stu)
        self.assertEqual((c["status"], c["locked"]), ("in_progress", False))
        self.assertEqual(self.client.get(f"/api/student/attempts/{a}/").status_code, 200)
        self.client.post(f"/api/student/attempts/{a}/submit/")
        self.assertTrue(self.card(self.stu)["locked"])  # a retake needs access again

    def test_free_tests_are_not_affected(self):
        free = self.make_exam()
        self.assertFalse(self.card(self.stu, free)["locked"])

    def test_admin_sets_premium_and_price_and_rejects_a_negative_price(self):
        self.client.force_authenticate(self.admin)
        draft = self.make_exam(publish=False)
        r = self.client.patch(f"/api/exams/{draft.id}/", {"is_premium": True, "price": "99.50"}, format="json")
        self.assertEqual((r.status_code, r.data["is_premium"], r.data["price"]), (200, True, "99.50"))
        self.assertEqual(self.client.patch(f"/api/exams/{draft.id}/", {"price": "-1"}, format="json").status_code, 400)

    def test_access_can_be_changed_after_publishing_and_duplicates_keep_it(self):
        self.grant(students=[self.stu.id])
        self.client.force_authenticate(self.admin)
        copy = self.client.post(f"/api/exams/{self.premium.id}/duplicate/")
        self.assertEqual(copy.status_code, 201, copy.content)
        self.assertEqual(copy.data["premium_student_ids"], [self.stu.id])

    def test_students_cannot_grant_themselves_access(self):
        self.client.force_authenticate(self.stu)
        r = self.client.put(f"/api/exams/{self.premium.id}/premium-access/", {"students": [self.stu.id]}, format="json")
        self.assertEqual(r.status_code, 403)


class PublicExamPageTests(APITestCase):
    def setUp(self):
        self.ssc = ExamCategory.objects.create(name="SSC", description="Staff Selection Commission")

    def test_shows_only_open_published_tests_with_no_way_in(self):
        Exam.objects.create(title="Open", duration_minutes=10, status="published", category=self.ssc)
        Exam.objects.create(title="Draft", duration_minutes=10, status="draft", category=self.ssc)
        Exam.objects.create(title="Private", duration_minutes=10, status="published", access_type="assigned", category=self.ssc)
        r = self.client.get(f"/api/public/exams/{self.ssc.public_id}/")
        self.assertEqual(r.status_code, 200)
        self.assertEqual([t["title"] for t in r.data["tests"]], ["Open"])
        self.assertEqual(r.data["name"], "SSC")
        self.assertNotIn("public_id", str(r.data["tests"]))
        self.assertIn("max-age", r["Cache-Control"])

    def test_inactive_or_unknown_exams_are_not_found(self):
        self.ssc.is_active = False
        self.ssc.save()
        self.assertEqual(self.client.get(f"/api/public/exams/{self.ssc.public_id}/").status_code, 404)
        self.assertEqual(self.client.get("/api/public/exams/nope/").status_code, 404)

    def test_the_landing_page_exam_list_carries_the_id_for_the_link(self):
        Exam.objects.create(title="Open", duration_minutes=10, status="published", category=self.ssc)
        self.assertEqual(self.client.get("/api/public/home/").data["exams"][0]["id"], self.ssc.public_id)
