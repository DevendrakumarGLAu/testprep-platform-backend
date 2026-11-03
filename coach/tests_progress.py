from datetime import timedelta
from decimal import Decimal

from django.utils import timezone

from accounts.models import Role
from attempts.models import Attempt
from attempts.tests import EngineBase, user
from exams.models import Batch

from .models import StudyPlan, StudyTask


class BatchProgressTests(EngineBase):
    def setUp(self):
        super().setUp()
        self.batch = Batch.objects.create(name="Evening batch")
        self.names = {n: user(n) for n in ("idle", "rising", "slipping", "fresh", "steady")}
        self.batch.students.add(*self.names.values())
        self.now = timezone.now()
        self.number = 0

    def attempt(self, student, percent, days_ago, practice=False):
        self.number += 1
        a = Attempt.objects.create(
            exam=self.exam, student=student, attempt_number=self.number, exam_version=1, status=Attempt.Status.SUBMITTED,
            deadline=self.now, duration_minutes=30, total_marks=Decimal("100"), passing_marks=Decimal("33"), negative_marking_enabled=False,
            score=Decimal(str(percent)), submitted_at=self.now,
        )
        when = self.now - timedelta(days=days_ago)
        Attempt.objects.filter(pk=a.pk).update(started_at=when, submitted_at=when)
        return a

    def report(self, **query):
        self.client.force_authenticate(self.admin)
        return self.client.get(f"/api/admin/batches/{self.batch.id}/progress/", query)

    def status_of(self, name, **query):
        return next(r for r in self.report(**query).data["results"] if r["username"] == name)

    def fill(self):
        n = self.names
        for i, p in enumerate([40, 42, 45, 60, 65, 70]):  # climbs
            self.attempt(n["rising"], p, 10 - i * 2)
        for i, p in enumerate([80, 78, 75, 60, 55, 50]):  # falls
            self.attempt(n["slipping"], p, 10 - i * 2)
        for i, p in enumerate([60, 62, 59, 61, 60, 63]):  # flat
            self.attempt(n["steady"], p, 10 - i * 2)
        self.attempt(n["idle"], 70, 25)  # nothing for 25 days

    def test_statuses(self):
        self.fill()
        self.assertEqual(self.status_of("rising")["status"], "improving")
        self.assertEqual(self.status_of("slipping")["status"], "declining")
        self.assertEqual(self.status_of("steady")["status"], "steady")
        self.assertEqual(self.status_of("idle")["status"], "behind")
        self.assertEqual(self.status_of("fresh")["status"], "new")

    def test_summary_and_order(self):
        self.fill()
        data = self.report().data
        self.assertEqual(data["students"], 5)
        self.assertEqual(data["counts"], {"behind": 1, "declining": 1, "steady": 1, "improving": 1, "new": 1})
        self.assertEqual(data["results"][0]["username"], "idle")  # who needs help comes first
        self.assertEqual(data["results"][-1]["username"], "fresh")
        rising = self.status_of("rising")
        self.assertEqual(len(rising["series"]), 6)
        self.assertGreater(rising["trend"], 0)

    def test_missed_study_plan_tasks_make_a_student_behind(self):
        n = self.names
        for i, p in enumerate([60, 61, 62]):
            self.attempt(n["steady"], p, 2 - i if i < 2 else 0)
        plan = StudyPlan.objects.create(student=n["steady"], target_name="SSC", exam_date=timezone.localdate() + timedelta(days=20))
        for d in range(1, 5):
            StudyTask.objects.create(plan=plan, date=timezone.localdate() - timedelta(days=d), kind="revise", title="Revise")
        row = self.status_of("steady")
        self.assertEqual(row["status"], "behind")
        self.assertEqual(row["plan_overdue"], 4)

    def test_practice_sets_do_not_count_as_scores(self):
        n = self.names
        self.exam.is_practice = True
        self.exam.save()
        self.attempt(n["steady"], 90, 1)
        self.assertEqual(self.status_of("steady")["average_percent"], None)

    def test_window_changes_the_average(self):
        n = self.names
        self.attempt(n["steady"], 90, 2)
        self.attempt(n["steady"], 30, 20)
        self.assertEqual(self.status_of("steady", days=7)["average_percent"], 90.0)
        self.assertEqual(self.status_of("steady", days=30)["average_percent"], 60.0)

    def test_csv_download(self):
        self.fill()
        r = self.report(csv=1)
        self.assertEqual(r["Content-Type"], "text/csv")
        text = r.content.decode()
        self.assertTrue(text.startswith("Name,Username,Status"))
        self.assertIn("idle", text)

    def test_students_and_unknown_batches(self):
        self.client.force_authenticate(self.names["rising"])
        self.assertEqual(self.client.get(f"/api/admin/batches/{self.batch.id}/progress/").status_code, 403)
        self.client.force_authenticate(self.admin)
        self.assertEqual(self.client.get("/api/admin/batches/9999/progress/").status_code, 404)
        self.assertEqual(self.client.get(f"/api/admin/batches/{self.batch.id}/progress/?days=abc").status_code, 400)

    def test_inactive_students_of_the_batch_are_left_out(self):
        self.names["fresh"].is_active = False
        self.names["fresh"].save()
        self.assertEqual(self.report().data["students"], 4)
