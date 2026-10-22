import io
from datetime import timedelta

from django.utils import timezone
from pypdf import PdfReader
from rest_framework.test import APITestCase

from accounts.models import ActivityLog, Role, User
from attempts import services as svc
from attempts.models import AttemptQuestion
from exams.models import Batch, Exam, ExamQuestion, ExamSection
from questionbank.models import Question, QuestionOption, Subject

PW = "Str0ng!pass99"


def user(name, role=Role.STUDENT, **extra):
    return User.objects.create_user(username=name, email=f"{name}@x.com", password=PW, role=role, first_name=name.title(), **extra)


def pdf_text(content: bytes) -> str:
    return "\n".join(page.extract_text() for page in PdfReader(io.BytesIO(content)).pages)


class ReportBase(APITestCase):
    def setUp(self):
        self.admin = user("adm", Role.ADMIN)
        self.alice = user("alice")
        self.bob = user("bob")
        self.geo = Subject.objects.create(name="Geo")
        self.sci = Subject.objects.create(name="Sci")
        self.exam = self.make_exam("Mock A")
        self.client.force_authenticate(self.admin)

    def make_exam(self, title, **over):
        defaults = dict(title=title, duration_minutes=30, passing_marks=3, max_attempts=0, status="published",
                        result_release="manual")  # unreleased results must still be visible to admins
        defaults.update(over)
        exam = Exam.objects.create(**defaults)
        sec = ExamSection.objects.create(exam=exam, name="S", order=0)
        for i, subject in enumerate((self.geo, self.geo, self.sci, self.sci)):
            q = Question.objects.create(text=f"{title} q{i}", subject=subject)
            QuestionOption.objects.create(question=q, text="right", is_correct=True, order=0)
            QuestionOption.objects.create(question=q, text="wrong", is_correct=False, order=1)
            ExamQuestion.objects.create(section=sec, question=q, order=i, marks=2, negative_marks=1, question_version=1)
        return exam

    def attempt(self, student, exam=None, plan=("c", "w", "c", None)):
        a, _ = svc.start_attempt(exam or self.exam, student)
        for aq, choice in zip(AttemptQuestion.objects.filter(attempt=a), plan):
            if choice:
                aq.selected_option_id = next(o["id"] for o in aq.options if o["is_correct"] == (choice == "c"))
                aq.save()
        return svc.submit_attempt(a.id)


class PermissionTests(ReportBase):
    def test_students_and_anonymous_blocked(self):
        urls = ["/api/admin/dashboard/", "/api/students/", f"/api/students/{self.alice.id}/", f"/api/students/{self.alice.id}/report.pdf"]
        self.client.force_authenticate(self.alice)
        for u in urls:
            self.assertEqual(self.client.get(u).status_code, 403, u)
        self.assertEqual(self.client.post("/api/reports/consolidated.pdf", {"student_ids": [self.bob.id]}, format="json").status_code, 403)
        self.assertEqual(self.client.post(f"/api/students/{self.alice.id}/assign-tests/", {"exam_ids": [self.exam.id]}, format="json").status_code, 403)
        self.client.force_authenticate(None)
        self.assertEqual(self.client.get("/api/admin/dashboard/").status_code, 401)


class DashboardTests(ReportBase):
    def test_numbers_come_from_the_database(self):
        self.make_exam("Draft one", status="draft")
        self.alice.is_active = False
        self.alice.save()
        self.attempt(self.alice)                              # 3/8 = 37.5%, passed
        self.attempt(self.bob, plan=("c", "c", "c", "c"))     # 100%
        svc.start_attempt(self.exam, self.bob)                # one still running
        d = self.client.get("/api/admin/dashboard/").data
        self.assertEqual(d["students"], {"total": 2, "active": 1, "new_last_30_days": 2})
        self.assertEqual(d["questions"], {"total": 8, "active": 8})
        self.assertEqual(d["tests"], {"total": 2, "published": 1, "draft": 1, "archived": 0})
        self.assertEqual(d["attempts"], {"submitted": 2, "in_progress": 1, "participating_students": 2})
        self.assertEqual(d["average_percentage"], 68.8)
        self.assertEqual(d["pass_rate"], 100.0)
        self.assertEqual(len(d["participation"]), 14)
        self.assertEqual(d["participation"][-1]["attempts"], 2)
        self.assertEqual(sum(b["count"] for b in d["score_distribution"]), 2)
        self.assertEqual(d["score_distribution"][3]["count"], 1)    # 30-40%
        self.assertEqual(d["score_distribution"][9]["count"], 1)    # 90-100%
        geo = next(s for s in d["subjects"] if s["subject"] == "Geo")
        self.assertEqual(geo["accuracy"], 75.0)  # alice 1/2, bob 2/2
        self.assertEqual(d["top_tests"][0]["attempts"], 2)
        self.assertEqual(len(d["recent_activity"]), 2)

    def test_empty_platform(self):
        d = self.client.get("/api/admin/dashboard/").data
        self.assertEqual((d["students"]["total"], d["attempts"]["submitted"]), (2, 0))
        self.assertIsNone(d["average_percentage"])
        self.assertEqual(d["recent_activity"], [])


class StudentManagementTests(ReportBase):
    def test_list_only_students_with_stats(self):
        self.attempt(self.alice)
        self.attempt(self.alice, plan=("c", "c", "c", "c"))
        r = self.client.get("/api/students/")
        self.assertEqual(r.data["count"], 2)
        rows = {x["username"]: x for x in r.data["results"]}
        self.assertNotIn("adm", rows)
        self.assertEqual(rows["alice"]["attempts_count"], 2)
        self.assertEqual(rows["alice"]["average_percentage"], 68.8)
        self.assertEqual(rows["bob"]["attempts_count"], 0)
        self.assertIsNone(rows["bob"]["average_percentage"])

    def test_filters_search_and_ordering(self):
        b = Batch.objects.create(name="B1")
        b.students.add(self.alice)
        self.bob.is_active = False
        self.bob.save()
        User.objects.filter(pk=self.bob.pk).update(date_joined=timezone.now() - timedelta(days=60))
        names = lambda q: sorted(x["username"] for x in self.client.get(f"/api/students/?{q}").data["results"])  # noqa: E731
        self.assertEqual(names(f"batch={b.id}"), ["alice"])
        self.assertEqual(names("is_active=false"), ["bob"])
        self.assertEqual(names("is_active=true"), ["alice"])
        self.assertEqual(names("search=bob"), ["bob"])
        self.assertEqual(names("search=alice@x.com"), ["alice"])
        cutoff = (timezone.now() - timedelta(days=30)).date().isoformat()
        self.assertEqual(names(f"registered_from={cutoff}"), ["alice"])
        self.assertEqual(names(f"registered_to={cutoff}"), ["bob"])
        self.attempt(self.alice)
        order = [x["username"] for x in self.client.get("/api/students/?ordering=-attempts_count").data["results"]]
        self.assertEqual(order[0], "alice")

    def test_batch_filter_does_not_duplicate_students(self):
        for n in ("B1", "B2"):
            Batch.objects.create(name=n).students.add(self.alice)
        self.attempt(self.alice)
        r = self.client.get(f"/api/students/?batch={Batch.objects.get(name='B1').id}")
        self.assertEqual((r.data["count"], r.data["results"][0]["attempts_count"]), (1, 1))
        self.assertEqual(len(r.data["results"][0]["batches"]), 2)

    def test_profile_includes_unreleased_results_and_history(self):
        self.attempt(self.alice)
        svc.start_attempt(self.exam, self.alice)  # running attempt
        d = self.client.get(f"/api/students/{self.alice.id}/").data
        perf = d["performance"]
        self.assertEqual(d["name"], "Alice")
        self.assertEqual((perf["attempts_count"], perf["in_progress_count"]), (1, 1))
        self.assertEqual((perf["average_percentage"], perf["best_percentage"], perf["pass_rate"]), (37.5, 37.5, 100.0))
        self.assertEqual(perf["accuracy_percent"], 66.7)
        self.assertEqual([s["subject"] for s in perf["subjects"]], ["Geo", "Sci"])
        self.assertEqual(len(perf["history"]), 2)
        self.assertEqual(perf["history"][1]["score"], "3.00")  # visible although the test releases results manually
        self.assertEqual(len(perf["trend"]), 1)

    def test_admin_ids_are_not_students(self):
        self.assertEqual(self.client.get(f"/api/students/{self.admin.id}/").status_code, 404)

    def test_assign_tests_individually(self):
        assigned = self.make_exam("Assigned test", access_type="assigned")
        draft = self.make_exam("Draft", access_type="assigned", status="draft")
        archived = self.make_exam("Gone", access_type="assigned", status="archived")
        url = f"/api/students/{self.alice.id}/assign-tests/"
        r = self.client.post(url, {"exam_ids": [assigned.id, draft.id, archived.id, self.exam.id]}, format="json")
        self.assertEqual(r.status_code, 200, r.content)
        self.assertEqual(sorted(e["title"] for e in r.data["assigned_exams"]), ["Assigned test", "Draft", "Mock A"])
        self.assertEqual(len(r.data["notes"]), 1)
        self.assertIn("Mock A", r.data["notes"][0])  # it is public, so assigning changes nothing
        # The assigned student now sees the test; the other student does not.
        self.client.force_authenticate(self.alice)
        self.assertIn("Assigned test", [c["title"] for c in self.client.get("/api/student/exams/").data])
        self.client.force_authenticate(self.bob)
        self.assertNotIn("Assigned test", [c["title"] for c in self.client.get("/api/student/exams/").data])
        # Remove again.
        self.client.force_authenticate(self.admin)
        r = self.client.post(url, {"exam_ids": [assigned.id], "mode": "remove"}, format="json")
        self.assertNotIn("Assigned test", [e["title"] for e in r.data["assigned_exams"]])
        self.assertEqual(self.client.post(url, {"exam_ids": []}, format="json").status_code, 400)
        self.assertEqual(self.client.post(url, {"exam_ids": [assigned.id], "mode": "x"}, format="json").status_code, 400)

    def test_set_batches(self):
        a, b = Batch.objects.create(name="A"), Batch.objects.create(name="B")
        url = f"/api/students/{self.alice.id}/batches/"
        r = self.client.put(url, {"batch_ids": [a.id, b.id]}, format="json")
        self.assertEqual(sorted(x["name"] for x in r.data["batches"]), ["A", "B"])
        r = self.client.put(url, {"batch_ids": [b.id]}, format="json")
        self.assertEqual([x["name"] for x in r.data["batches"]], ["B"])
        self.assertEqual(self.client.put(url, {"batch_ids": "x"}, format="json").status_code, 400)

    def test_activate_deactivate_still_available(self):
        self.assertEqual(self.client.delete(f"/api/users/{self.alice.id}/").status_code, 204)
        self.assertFalse(self.client.get(f"/api/students/{self.alice.id}/").data["is_active"])
        self.client.post(f"/api/users/{self.alice.id}/activate/")
        self.assertTrue(self.client.get(f"/api/students/{self.alice.id}/").data["is_active"])


class PdfReportTests(ReportBase):
    def test_individual_student_report(self):
        self.attempt(self.alice)
        r = self.client.get(f"/api/students/{self.alice.id}/report.pdf")
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r["Content-Type"], "application/pdf")
        self.assertIn("attachment", r["Content-Disposition"])
        self.assertTrue(r.content.startswith(b"%PDF"))
        text = pdf_text(r.content)
        for expected in ("Student performance report", "Alice", "alice@x.com", "Mock A", "37.5%", "Geo", "Subject-wise"):
            self.assertIn(expected, text)
        self.assertTrue(ActivityLog.objects.filter(action="report.student_pdf").exists())

    def test_report_for_student_without_attempts(self):
        r = self.client.get(f"/api/students/{self.bob.id}/report.pdf")
        self.assertEqual(r.status_code, 200)
        self.assertIn("No attempts yet", pdf_text(r.content))

    def test_special_characters_do_not_break_pdf(self):
        u = user("zed")
        u.first_name, u.last_name = "<b>Zed</b> & Co", "O'Neil"
        u.save()
        r = self.client.get(f"/api/students/{u.id}/report.pdf")
        self.assertEqual(r.status_code, 200)
        self.assertIn("<b>Zed</b> & Co", pdf_text(r.content))

    def test_report_for_non_student_is_404(self):
        self.assertEqual(self.client.get(f"/api/students/{self.admin.id}/report.pdf").status_code, 404)

    def test_consolidated_for_students(self):
        self.attempt(self.alice)
        self.attempt(self.bob, plan=("c", "c", "c", "c"))
        r = self.client.post("/api/reports/consolidated.pdf", {"student_ids": [self.alice.id, self.bob.id, self.admin.id]}, format="json")
        self.assertEqual(r.status_code, 200, r.content)
        self.assertTrue(r.content.startswith(b"%PDF"))
        text = pdf_text(r.content)
        self.assertIn("Consolidated student report", text)
        self.assertIn("2 student(s)", text)  # the admin id was ignored
        self.assertIn("Alice", text)
        self.assertIn("Bob", text)

    def test_consolidated_for_tests(self):
        other = self.make_exam("Mock B")
        self.attempt(self.alice)
        self.attempt(self.alice, plan=("c", "c", "c", "c"))
        self.attempt(self.bob, exam=other)
        r = self.client.post("/api/reports/consolidated.pdf", {"exam_ids": [self.exam.id, other.id]}, format="json")
        self.assertEqual(r.status_code, 200, r.content)
        text = pdf_text(r.content)
        self.assertIn("Consolidated test report", text)
        self.assertIn("Mock A", text)
        self.assertIn("Mock B", text)
        self.assertIn("Alice", text)
        self.assertIn("8.00", text)  # alice's best attempt

    def test_consolidated_validation(self):
        post = lambda body: self.client.post("/api/reports/consolidated.pdf", body, format="json").status_code  # noqa: E731
        self.assertEqual(post({}), 400)
        self.assertEqual(post({"student_ids": [self.alice.id], "exam_ids": [self.exam.id]}), 400)
        self.assertEqual(post({"student_ids": [99999]}), 400)
        self.assertEqual(post({"student_ids": list(range(1, 202))}), 400)
        self.assertEqual(post({"exam_ids": list(range(1, 32))}), 400)

    def test_only_pdf_is_offered_for_student_reports(self):
        """Student reports must not be exportable as CSV or Excel."""
        for url in (
            f"/api/students/{self.alice.id}/report.csv", f"/api/students/{self.alice.id}/report.xlsx",
            f"/api/students/{self.alice.id}/report.xls", "/api/reports/consolidated.csv", "/api/reports/consolidated.xlsx",
            "/api/students/export/", "/api/students/export.csv",
        ):
            self.assertEqual(self.client.get(url).status_code, 404, url)
        r = self.client.get("/api/students/?format=csv")
        self.assertNotIn("text/csv", r["Content-Type"])
        self.assertIn("json", r["Content-Type"])
