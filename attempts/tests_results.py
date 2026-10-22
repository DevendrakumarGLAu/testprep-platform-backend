from datetime import timedelta

from django.utils import timezone
from rest_framework.test import APITestCase

from accounts.models import ActivityLog, Role, User
from exams.models import Exam, ExamQuestion, ExamSection
from questionbank.models import Question, QuestionOption, Subject

from .models import Attempt, AttemptQuestion

PW = "Str0ng!pass99"


def user(name, role=Role.STUDENT):
    return User.objects.create_user(username=name, email=f"{name}@x.com", password=PW, role=role)


class ResultsBase(APITestCase):
    def setUp(self):
        self.admin = user("adm", Role.ADMIN)
        self.stu = user("stu")
        self.other = user("other")
        self.geo = Subject.objects.create(name="Geo")
        self.sci = Subject.objects.create(name="Sci")

    def make_exam(self, **over):
        defaults = dict(title="Mock", duration_minutes=30, passing_marks=3, max_attempts=3, status="published")
        defaults.update(over)
        exam = Exam.objects.create(**defaults)
        a = ExamSection.objects.create(exam=exam, name="Section A", order=0)
        b = ExamSection.objects.create(exam=exam, name="Section B", order=1)
        for sec, subject, n in ((a, self.geo, 2), (b, self.sci, 2)):
            for i in range(n):
                q = Question.objects.create(text=f"{subject.name} q{i} {exam.id}", subject=subject, explanation=f"why {i}")
                QuestionOption.objects.create(question=q, text="right", is_correct=True, order=0)
                QuestionOption.objects.create(question=q, text="wrong", is_correct=False, order=1)
                ExamQuestion.objects.create(section=sec, question=q, order=i, marks=2, negative_marks=1, question_version=1)
        return exam

    def take(self, exam, who=None, plan=("c", "w", "c", None)):
        """Attempt the test: c = correct, w = wrong, None = skip. Returns the attempt id."""
        self.client.force_authenticate(who or self.stu)
        a = self.client.post(f"/api/student/exams/{exam.public_id}/start/").data["attempt_id"]
        for aq, choice in zip(AttemptQuestion.objects.filter(attempt__public_id=a), plan):
            if choice is None:
                continue
            want = choice == "c"
            opt = next(o["id"] for o in aq.options if o["is_correct"] == want)
            self.client.put(f"/api/student/attempts/{a}/answers/", {"question": aq.id, "selected_option": opt}, format="json")
        self.client.post(f"/api/student/attempts/{a}/submit/")
        return a


class StudentResultTests(ResultsBase):
    def test_full_result_with_section_and_subject_marks(self):
        exam = self.make_exam()
        a = self.take(exam)
        r = self.client.get(f"/api/student/attempts/{a}/result/")
        self.assertEqual(r.status_code, 200)
        d = r.data
        # +2, -1 | +2, 0 (unanswered)
        self.assertEqual(d["summary"]["score"], "3.00")
        self.assertEqual(d["summary"]["percentage"], 37.5)
        self.assertTrue(d["summary"]["passed"])
        self.assertEqual([(s["name"], s["marks"], s["max_marks"]) for s in d["sections"]],
                         [("Section A", "1.00", "4.00"), ("Section B", "2.00", "4.00")])
        self.assertEqual(sum(float(s["marks"]) for s in d["sections"]), float(d["summary"]["score"]))
        self.assertEqual([s["subject"] for s in d["subjects"]], ["Geo", "Sci"])
        self.assertEqual(d["sections"][1]["unanswered"], 1)
        self.assertTrue(d["solutions_available"])
        q = d["questions"]
        self.assertEqual([x["status"] for x in q], ["correct", "incorrect", "correct", "unanswered"])
        self.assertEqual([x["marks_awarded"] for x in q], ["2.00", "-1.00", "2.00", "0.00"])
        self.assertTrue(all(x["explanation"] for x in q))
        self.assertTrue(q[1]["correct_option"] != q[1]["selected_option"])
        self.assertEqual(d["comparison"]["attempts_count"], 1)

    def test_result_not_available_before_submit_or_release(self):
        exam = self.make_exam(result_release="manual")
        self.client.force_authenticate(self.stu)
        a = self.client.post(f"/api/student/exams/{exam.public_id}/start/").data["attempt_id"]
        self.assertEqual(self.client.get(f"/api/student/attempts/{a}/result/").status_code, 409)
        self.client.post(f"/api/student/attempts/{a}/submit/")
        r = self.client.get(f"/api/student/attempts/{a}/result/")
        self.assertEqual(r.status_code, 403)
        self.assertNotIn("summary", r.data)

    def test_solutions_hidden_until_released_but_result_visible(self):
        exam = self.make_exam(solution_release="manual")
        a = self.take(exam)
        d = self.client.get(f"/api/student/attempts/{a}/result/").data
        self.assertEqual(d["summary"]["score"], "3.00")
        self.assertFalse(d["solutions_available"])
        body = str(d["questions"])
        for hidden in ("is_correct", "correct_option", "explanation", "marks_awarded", "status"):
            self.assertNotIn(hidden, body)
        self.assertIsNotNone(d["questions"][0]["selected_option"])  # their own choice is still shown

        Exam.objects.filter(pk=exam.pk).update(solutions_released=True)
        d = self.client.get(f"/api/student/attempts/{a}/result/").data
        self.assertTrue(d["solutions_available"])
        self.assertIn("explanation", d["questions"][0])

    def test_solutions_after_window_end(self):
        exam = self.make_exam(solution_release="after_end", end_at=timezone.now() + timedelta(hours=2))
        a = self.take(exam)
        self.assertFalse(self.client.get(f"/api/student/attempts/{a}/result/").data["solutions_available"])
        Exam.objects.filter(pk=exam.pk).update(end_at=timezone.now() - timedelta(minutes=1))
        self.assertTrue(self.client.get(f"/api/student/attempts/{a}/result/").data["solutions_available"])

    def test_other_students_result_is_not_accessible(self):
        exam = self.make_exam()
        a = self.take(exam)
        self.client.force_authenticate(self.other)
        self.assertEqual(self.client.get(f"/api/student/attempts/{a}/result/").status_code, 404)

    def test_result_reflects_snapshot_not_later_bank_edits(self):
        exam = self.make_exam()
        a = self.take(exam)
        Question.objects.update(text="EDITED")
        QuestionOption.objects.update(is_correct=False)
        d = self.client.get(f"/api/student/attempts/{a}/result/").data
        self.assertNotIn("EDITED", d["questions"][0]["text"])
        self.assertEqual(d["summary"]["score"], "3.00")

    def test_comparison_uses_all_students(self):
        exam = self.make_exam()
        self.take(exam, self.other, plan=("c", "c", "c", "c"))  # 8/8 = 100%
        a = self.take(exam, self.stu)  # 3/8 = 37.5%
        d = self.client.get(f"/api/student/attempts/{a}/result/").data
        self.assertEqual(d["comparison"], {"attempts_count": 2, "average_percentage": 68.8})


class AdminResultTests(ResultsBase):
    @staticmethod
    def pk(public_id):
        return Attempt.objects.get(public_id=public_id).pk

    def setUp(self):
        super().setUp()
        self.exam = self.make_exam(result_release="manual", solution_release="manual")
        self.a1 = self.take(self.exam, self.stu)                                  # 3/8
        self.a2 = self.take(self.exam, self.other, plan=("c", "c", "c", "c"))     # 8/8
        self.client.force_authenticate(self.admin)

    def test_students_cannot_use_admin_endpoints(self):
        self.client.force_authenticate(self.stu)
        for url in (f"/api/exams/{self.exam.id}/attempts/", f"/api/exams/{self.exam.id}/results-summary/", f"/api/attempts/{self.pk(self.a1)}/"):
            self.assertEqual(self.client.get(url).status_code, 403, url)
        self.assertEqual(self.client.post(f"/api/exams/{self.exam.id}/release-results/").status_code, 403)

    def test_attempts_list_ranked_with_search_and_filters(self):
        r = self.client.get(f"/api/exams/{self.exam.id}/attempts/")
        self.assertEqual(r.data["count"], 2)
        self.assertEqual([x["student_name"] for x in r.data["results"]], ["other", "stu"])  # best score first
        self.assertEqual(r.data["results"][0]["percentage"], 100.0)
        self.assertEqual(self.client.get(f"/api/exams/{self.exam.id}/attempts/?search=stu").data["count"], 1)
        self.assertEqual(self.client.get(f"/api/exams/{self.exam.id}/attempts/?passed=false").data["count"], 0)

    def test_admin_can_review_any_attempt_even_when_unreleased(self):
        d = self.client.get(f"/api/attempts/{self.pk(self.a1)}/").data
        self.assertTrue(d["solutions_available"])
        self.assertEqual(d["attempt"]["student"]["email"], "stu@x.com")
        self.assertEqual(d["summary"]["score"], "3.00")

    def test_in_progress_attempt_cannot_be_reviewed(self):
        self.client.force_authenticate(self.stu)
        b = self.client.post(f"/api/student/exams/{self.exam.public_id}/start/").data["attempt_id"]
        self.client.force_authenticate(self.admin)
        self.assertEqual(self.client.get(f"/api/attempts/{self.pk(b)}/").status_code, 409)

    def test_summary_statistics(self):
        d = self.client.get(f"/api/exams/{self.exam.id}/results-summary/").data
        self.assertEqual(d["attempts_submitted"], 2)
        self.assertEqual(d["students"], 2)
        self.assertEqual(d["highest_score"], "8.00")
        self.assertEqual(d["lowest_score"], "3.00")
        self.assertEqual(d["average_score"], "5.50")
        self.assertEqual(d["average_percentage"], 68.8)
        self.assertEqual(d["pass_rate"], 100.0)
        self.assertEqual(len(d["questions"]), 4)
        self.assertEqual(d["questions"][0]["correct_percent"], 50.0)  # the hardest question comes first
        self.assertFalse(d["exam"]["results_visible"])

    def test_manual_release_flow(self):
        base = f"/api/exams/{self.exam.id}"
        r = self.client.post(f"{base}/release-results/")
        self.assertEqual(r.status_code, 200)
        self.assertTrue(r.data["results_visible"])
        self.assertFalse(r.data["solutions_visible"])
        self.client.force_authenticate(self.stu)
        self.assertEqual(self.client.get(f"/api/student/attempts/{self.a1}/result/").status_code, 200)
        self.client.force_authenticate(self.admin)
        self.assertTrue(self.client.post(f"{base}/release-solutions/").data["solutions_visible"])
        self.assertFalse(self.client.post(f"{base}/hide-results/").data["results_visible"])
        self.client.force_authenticate(self.stu)
        self.assertEqual(self.client.get(f"/api/student/attempts/{self.a1}/result/").status_code, 403)
        actions = set(ActivityLog.objects.values_list("action", flat=True))
        self.assertTrue({"exam.release_results", "exam.release_solutions", "exam.hide_results"} <= actions)

    def test_release_controls_require_manual_policy(self):
        other = self.make_exam(title="Auto")  # immediate + with_result
        self.assertEqual(self.client.post(f"/api/exams/{other.id}/release-results/").status_code, 400)
        self.assertEqual(self.client.post(f"/api/exams/{other.id}/release-solutions/").status_code, 400)

    def test_dashboard_only_counts_results_after_release(self):
        self.client.force_authenticate(self.stu)
        self.assertIsNone(self.client.get("/api/student/dashboard/").data["average_score_percent"])
        Exam.objects.filter(pk=self.exam.pk).update(results_released=True)
        self.assertEqual(self.client.get("/api/student/dashboard/").data["average_score_percent"], 37.5)
        self.assertEqual(Attempt.objects.filter(student=self.stu).count(), 1)
