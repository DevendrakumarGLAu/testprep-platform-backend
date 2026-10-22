from datetime import timedelta
from io import StringIO

from django.core.management import call_command
from django.utils import timezone
from rest_framework.test import APITestCase

from accounts.models import Role, User
from exams.models import Batch, Exam, ExamQuestion, ExamSection
from questionbank.models import Question, QuestionOption, Subject

from .models import Attempt

PW = "Str0ng!pass99"


def user(name, role=Role.STUDENT):
    return User.objects.create_user(username=name, email=f"{name}@x.com", password=PW, role=role)


class EngineBase(APITestCase):
    def setUp(self):
        self.admin = user("adm", Role.ADMIN)
        self.stu = user("stu")
        self.other = user("other")
        self.subject = Subject.objects.create(name="Geo")
        self.exam = self.make_exam()

    def make_question(self, text):
        q = Question.objects.create(text=text, subject=self.subject, explanation="because", marks=2, negative_marks=1)
        QuestionOption.objects.create(question=q, text="right", is_correct=True, order=0)
        QuestionOption.objects.create(question=q, text="wrong", is_correct=False, order=1)
        return q

    def make_exam(self, n=3, publish=True, **over):
        defaults = dict(title="Mock", duration_minutes=30, passing_marks=2, max_attempts=2, status=Exam.Status.PUBLISHED if publish else Exam.Status.DRAFT)
        defaults.update(over)
        exam = Exam.objects.create(**defaults)
        sec = ExamSection.objects.create(exam=exam, name="S1", order=0)
        for i in range(n):
            q = self.make_question(f"Question {Exam.objects.count()}-{i}")
            ExamQuestion.objects.create(section=sec, question=q, order=i, marks=2, negative_marks=1, question_version=q.version)
        return exam

    def login(self, u=None):
        self.client.force_authenticate(u or self.stu)

    def start(self, exam=None):
        r = self.client.post(f"/api/student/exams/{(exam or self.exam).public_id}/start/")
        self.assertIn(r.status_code, (200, 201), r.content)
        return r.data["attempt_id"]

    def questions(self, attempt_id):
        r = self.client.get(f"/api/student/attempts/{attempt_id}/")
        return [q for s in r.data["sections"] for q in s["questions"]]

    def correct_option(self, q_id):
        from .models import AttemptQuestion
        return next(o["id"] for o in AttemptQuestion.objects.get(pk=q_id).options if o["is_correct"])

    def wrong_option(self, q_id):
        from .models import AttemptQuestion
        return next(o["id"] for o in AttemptQuestion.objects.get(pk=q_id).options if not o["is_correct"])

    def answer(self, attempt_id, q_id, option):
        return self.client.put(f"/api/student/attempts/{attempt_id}/answers/", {"question": q_id, "selected_option": option}, format="json")


class EligibilityTests(EngineBase):
    def test_only_students_can_use_student_api(self):
        self.client.force_authenticate(self.admin)
        self.assertEqual(self.client.get("/api/student/exams/").status_code, 403)
        self.client.force_authenticate(None)
        self.assertEqual(self.client.get("/api/student/exams/").status_code, 401)

    def test_drafts_and_archived_are_invisible(self):
        self.make_exam(publish=False, title="Draft")
        self.make_exam(status=Exam.Status.ARCHIVED, title="Old")
        self.login()
        self.assertEqual([c["title"] for c in self.client.get("/api/student/exams/").data], ["Mock"])

    def test_assigned_and_batch_access(self):
        mine = self.make_exam(title="Assigned", access_type="assigned")
        mine.assigned_students.add(self.stu)
        batch_exam = self.make_exam(title="BatchOnly", access_type="batch")
        b = Batch.objects.create(name="B1")
        b.students.add(self.stu)
        batch_exam.assigned_batches.add(b)
        self.login()
        titles = {c["title"] for c in self.client.get("/api/student/exams/").data}
        self.assertEqual(titles, {"Mock", "Assigned", "BatchOnly"})
        self.login(self.other)
        titles = {c["title"] for c in self.client.get("/api/student/exams/").data}
        self.assertEqual(titles, {"Mock"})
        # Direct access by id is refused for non-eligible students.
        self.assertEqual(self.client.get(f"/api/student/exams/{mine.public_id}/").status_code, 404)
        self.assertEqual(self.client.post(f"/api/student/exams/{mine.public_id}/start/").status_code, 404)

    def test_inactive_batch_does_not_grant_access(self):
        e = self.make_exam(title="BatchOnly", access_type="batch")
        b = Batch.objects.create(name="B1", is_active=False)
        b.students.add(self.stu)
        e.assigned_batches.add(b)
        self.login()
        self.assertNotIn("BatchOnly", {c["title"] for c in self.client.get("/api/student/exams/").data})

    def test_upcoming_and_closed_windows_block_start(self):
        now = timezone.now()
        up = self.make_exam(title="Soon", start_at=now + timedelta(hours=1))
        closed = self.make_exam(title="Gone", end_at=now - timedelta(hours=1))
        self.login()
        cards = {c["title"]: c for c in self.client.get("/api/student/exams/").data}
        self.assertEqual(cards["Soon"]["status"], "upcoming")
        self.assertEqual(cards["Gone"]["status"], "missed")
        self.assertEqual(self.client.post(f"/api/student/exams/{up.public_id}/start/").status_code, 403)
        self.assertEqual(self.client.post(f"/api/student/exams/{closed.public_id}/start/").status_code, 403)

    def test_status_and_subject_filters(self):
        self.login()
        self.assertEqual(len(self.client.get("/api/student/exams/?status=available").data), 1)
        self.assertEqual(len(self.client.get("/api/student/exams/?status=completed").data), 0)
        self.assertEqual(len(self.client.get(f"/api/student/exams/?subject={self.subject.id}").data), 1)
        self.assertEqual(len(self.client.get("/api/student/exams/?subject=9999").data), 0)

    def test_max_attempts_enforced(self):
        self.login()
        for _ in range(2):
            a = self.start()
            self.client.post(f"/api/student/attempts/{a}/submit/")
        r = self.client.post(f"/api/student/exams/{self.exam.public_id}/start/")
        self.assertEqual(r.status_code, 403)
        self.assertEqual(self.client.get("/api/student/exams/").data[0]["status"], "completed")


class StartAndResumeTests(EngineBase):
    def test_start_is_idempotent_and_resumes(self):
        self.login()
        r1 = self.client.post(f"/api/student/exams/{self.exam.public_id}/start/")
        r2 = self.client.post(f"/api/student/exams/{self.exam.public_id}/start/")
        self.assertEqual((r1.status_code, r2.status_code), (201, 200))
        self.assertEqual(r1.data["attempt_id"], r2.data["attempt_id"])
        self.assertEqual(Attempt.objects.count(), 1)
        self.assertEqual(self.client.get("/api/student/exams/").data[0]["status"], "in_progress")

    def test_server_sets_deadline_and_never_exposes_answers(self):
        self.login()
        a = self.start()
        r = self.client.get(f"/api/student/attempts/{a}/")
        att = Attempt.objects.get(public_id=a)
        self.assertAlmostEqual(r.data["remaining_seconds"], 30 * 60, delta=5)
        self.assertAlmostEqual((att.deadline - att.started_at).total_seconds(), 30 * 60, delta=2)
        body = r.content.decode()
        self.assertNotIn("is_correct", body)
        self.assertNotIn("explanation", body)
        self.assertNotIn("because", body)

    def test_deadline_capped_by_window_end(self):
        e = self.make_exam(title="Short", end_at=timezone.now() + timedelta(minutes=10))
        self.login()
        a = self.start(e)
        self.assertLessEqual(self.client.get(f"/api/student/attempts/{a}/").data["remaining_seconds"], 10 * 60)

    def test_attempt_belongs_to_student(self):
        self.login()
        a = self.start()
        q = self.questions(a)[0]
        self.login(self.other)
        self.assertEqual(self.client.get(f"/api/student/attempts/{a}/").status_code, 404)
        self.assertEqual(self.answer(a, q["id"], q["options"][0]["id"]).status_code, 404)
        self.assertEqual(self.client.post(f"/api/student/attempts/{a}/submit/").status_code, 404)

    def test_attempt_snapshot_survives_bank_edits(self):
        self.login()
        a = self.start()
        original = self.questions(a)[0]["text"]
        Question.objects.update(text="CHANGED", marks=50)
        QuestionOption.objects.update(is_correct=False)
        self.assertEqual(self.questions(a)[0]["text"], original)

    def test_exam_with_attempts_cannot_be_unpublished(self):
        self.login()
        self.start()
        self.assertTrue(self.exam.has_attempts())
        self.client.force_authenticate(self.admin)
        self.assertEqual(self.client.post(f"/api/exams/{self.exam.id}/unpublish/").status_code, 400)

    def test_shuffle_keeps_all_questions_and_options(self):
        e = self.make_exam(title="Shuf", n=6, shuffle_questions=True, shuffle_options=True)
        self.login()
        a = self.start(e)
        qs = self.questions(a)
        self.assertEqual(len(qs), 6)
        self.assertEqual([q["number"] for q in qs], list(range(1, 7)))
        self.assertTrue(all(len(q["options"]) == 2 for q in qs))


class AnswerTests(EngineBase):
    def setUp(self):
        super().setUp()
        self.login()
        self.a = self.start()
        self.qs = self.questions(self.a)

    def test_save_change_clear_and_mark(self):
        q = self.qs[0]
        right = self.correct_option(q["id"])
        r = self.answer(self.a, q["id"], right)
        self.assertEqual(r.status_code, 200)
        self.assertIn("remaining_seconds", r.data)
        self.client.put(f"/api/student/attempts/{self.a}/answers/", {"question": q["id"], "marked_for_review": True}, format="json")
        got = self.questions(self.a)[0]
        self.assertEqual((got["selected_option"], got["marked_for_review"]), (right, True))
        self.answer(self.a, q["id"], None)
        self.assertIsNone(self.questions(self.a)[0]["selected_option"])

    def test_batch_save_after_reconnect(self):
        items = [{"question": q["id"], "selected_option": self.correct_option(q["id"])} for q in self.qs]
        r = self.client.put(f"/api/student/attempts/{self.a}/answers/", {"answers": items}, format="json")
        self.assertEqual(r.data["saved"], 3)

    def test_rejects_foreign_question_and_foreign_option(self):
        other_exam = self.make_exam(title="Other")
        self.login()
        b = self.start(other_exam)
        foreign_q = self.questions(b)[0]
        self.assertEqual(self.answer(self.a, foreign_q["id"], foreign_q["options"][0]["id"]).status_code, 400)
        q = self.qs[0]
        self.assertEqual(self.answer(self.a, q["id"], foreign_q["options"][0]["id"]).status_code, 400)

    def test_answer_after_deadline_is_rejected_and_attempt_auto_submitted(self):
        Attempt.objects.filter(public_id=self.a).update(deadline=timezone.now() - timedelta(seconds=60))
        q = self.qs[0]
        r = self.answer(self.a, q["id"], self.correct_option(q["id"]))
        self.assertEqual(r.status_code, 409)
        att = Attempt.objects.get(public_id=self.a)
        self.assertEqual(att.status, "submitted")
        self.assertTrue(att.auto_submitted)
        self.assertEqual(att.unanswered_count, 3)  # the late answer was not stored

    def test_small_grace_window_accepts_a_save_at_the_buzzer(self):
        Attempt.objects.filter(public_id=self.a).update(deadline=timezone.now() - timedelta(seconds=2))
        q = self.qs[0]
        self.assertEqual(self.answer(self.a, q["id"], self.correct_option(q["id"])).status_code, 200)

    def test_no_edits_after_submission(self):
        self.client.post(f"/api/student/attempts/{self.a}/submit/")
        q = self.qs[0]
        self.assertEqual(self.answer(self.a, q["id"], self.correct_option(q["id"])).status_code, 409)


class ScoringTests(EngineBase):
    def test_scoring_positive_negative_unanswered(self):
        e = self.make_exam(title="Scored", n=4, unanswered_penalty="0.5", passing_marks=1)
        self.login()
        a = self.start(e)
        qs = self.questions(a)
        self.answer(a, qs[0]["id"], self.correct_option(qs[0]["id"]))   # +2
        self.answer(a, qs[1]["id"], self.correct_option(qs[1]["id"]))   # +2
        self.answer(a, qs[2]["id"], self.wrong_option(qs[2]["id"]))     # -1
        # qs[3] unanswered: -0.5
        r = self.client.post(f"/api/student/attempts/{a}/submit/")
        res = r.data["result"]
        self.assertEqual(res["score"], "2.50")
        self.assertEqual((res["correct"], res["incorrect"], res["unanswered"]), (2, 1, 1))
        self.assertEqual(res["accuracy"], "66.67")
        self.assertEqual(res["total_marks"], "8.00")
        self.assertTrue(res["passed"])

    def test_negative_marking_can_be_disabled(self):
        e = self.make_exam(title="NoNeg", n=2, negative_marking_enabled=False)
        self.login()
        a = self.start(e)
        qs = self.questions(a)
        self.answer(a, qs[0]["id"], self.wrong_option(qs[0]["id"]))
        res = self.client.post(f"/api/student/attempts/{a}/submit/").data["result"]
        self.assertEqual(res["score"], "0.00")

    def test_fail_when_below_passing(self):
        e = self.make_exam(title="Hard", n=2, passing_marks=4)
        self.login()
        a = self.start(e)
        qs = self.questions(a)
        self.answer(a, qs[0]["id"], self.correct_option(qs[0]["id"]))
        self.assertFalse(self.client.post(f"/api/student/attempts/{a}/submit/").data["result"]["passed"])

    def test_submit_is_idempotent(self):
        self.login()
        a = self.start()
        qs = self.questions(a)
        self.answer(a, qs[0]["id"], self.correct_option(qs[0]["id"]))
        first = self.client.post(f"/api/student/attempts/{a}/submit/").data
        att = Attempt.objects.get(public_id=a)
        stamp = att.submitted_at
        second = self.client.post(f"/api/student/attempts/{a}/submit/").data
        self.assertEqual(first["result"], second["result"])
        self.assertEqual(Attempt.objects.get(public_id=a).submitted_at, stamp)

    def test_client_cannot_supply_a_score(self):
        self.login()
        a = self.start()
        r = self.client.post(f"/api/student/attempts/{a}/submit/", {"score": 999, "passed": True}, format="json")
        self.assertEqual(r.data["result"]["score"], "0.00")  # nothing answered, no penalty
        self.assertEqual(r.data["result"]["correct"], 0)

    def test_results_hidden_until_released(self):
        e = self.make_exam(title="Manual", result_release="manual")
        self.login()
        a = self.start(e)
        r = self.client.post(f"/api/student/attempts/{a}/submit/")
        self.assertFalse(r.data["result_available"])
        self.assertIsNone(r.data["result"])
        hist = self.client.get("/api/student/attempts/").data[0]
        self.assertIsNone(hist["score"])
        e.results_released = True
        e.save()
        self.assertTrue(self.client.get(f"/api/student/attempts/{a}/").data["result_available"])

    def test_scheduled_release(self):
        e = self.make_exam(title="Sched", result_release="scheduled", result_release_at=timezone.now() + timedelta(hours=1))
        self.login()
        a = self.start(e)
        self.assertFalse(self.client.post(f"/api/student/attempts/{a}/submit/").data["result_available"])
        Exam.objects.filter(pk=e.pk).update(result_release_at=timezone.now() - timedelta(minutes=1))
        self.assertTrue(self.client.get(f"/api/student/attempts/{a}/").data["result_available"])

    def test_time_taken_is_capped_at_duration(self):
        self.login()
        a = self.start()
        Attempt.objects.filter(public_id=a).update(
            started_at=timezone.now() - timedelta(hours=3), deadline=timezone.now() - timedelta(hours=2)
        )
        res = self.client.get(f"/api/student/attempts/{a}/").data["result"]
        self.assertEqual(res["time_taken_seconds"], 30 * 60)


class AutoSubmitTests(EngineBase):
    def test_management_command_submits_overdue_attempts(self):
        self.login()
        a = self.start()
        Attempt.objects.filter(public_id=a).update(deadline=timezone.now() - timedelta(minutes=1))
        out = StringIO()
        call_command("auto_submit_expired", stdout=out)
        self.assertIn("1 expired", out.getvalue())
        att = Attempt.objects.get(public_id=a)
        self.assertEqual((att.status, att.auto_submitted), ("submitted", True))
        self.assertLessEqual(att.submitted_at, att.deadline)

    def test_listing_sweeps_expired_attempts(self):
        self.login()
        a = self.start()
        Attempt.objects.filter(public_id=a).update(deadline=timezone.now() - timedelta(minutes=1))
        self.assertEqual(self.client.get("/api/student/exams/").data[0]["status"], "available")  # swept, attempt 2 now possible
        self.assertEqual(Attempt.objects.get(public_id=a).status, "submitted")


class DashboardTests(EngineBase):
    def test_dashboard_reflects_only_own_released_results(self):
        self.login()
        a = self.start()
        qs = self.questions(a)
        self.answer(a, qs[0]["id"], self.correct_option(qs[0]["id"]))
        self.answer(a, qs[1]["id"], self.wrong_option(qs[1]["id"]))
        self.client.post(f"/api/student/attempts/{a}/submit/")

        d = self.client.get("/api/student/dashboard/").data
        self.assertEqual(d["counts"]["completed"], 1)
        self.assertEqual(d["accuracy_percent"], 50.0)
        self.assertEqual(d["average_score_percent"], 16.7)  # (2 - 1) / 6
        self.assertEqual(d["subjects"][0]["subject"], "Geo")
        self.assertEqual(len(d["trend"]), 1)
        self.assertEqual(len(d["recent_results"]), 1)

        self.login(self.other)
        d = self.client.get("/api/student/dashboard/").data
        self.assertEqual(d["counts"]["completed"], 0)
        self.assertIsNone(d["average_score_percent"])
        self.assertEqual(len(d["recommended"]), 1)

    def test_unreleased_results_not_in_averages(self):
        e = self.make_exam(title="Manual", result_release="manual")
        Exam.objects.filter(pk=self.exam.pk).update(status="archived")
        self.login()
        a = self.start(e)
        self.client.post(f"/api/student/attempts/{a}/submit/")
        d = self.client.get("/api/student/dashboard/").data
        self.assertEqual(d["counts"]["completed"], 1)
        self.assertIsNone(d["average_score_percent"])
        self.assertEqual(d["recent_results"], [])


class UnlimitedAttemptTests(EngineBase):
    def test_zero_max_attempts_means_unlimited_retakes(self):
        e = self.make_exam(title="Retake", max_attempts=0)
        self.login()
        for i in range(1, 6):
            a = self.start(e)
            self.assertEqual(Attempt.objects.get(public_id=a).attempt_number, i)
            self.client.post(f"/api/student/attempts/{a}/submit/")
            card = next(c for c in self.client.get("/api/student/exams/").data if c["title"] == "Retake")
            self.assertEqual((card["status"], card["can_start"], card["attempts_used"]), ("available", True, i))
        # Still only one attempt can run at a time.
        a = self.start(e)
        self.assertEqual(self.start(e), a)

    def test_admin_can_set_unlimited_and_negative_is_rejected(self):
        self.client.force_authenticate(self.admin)
        ok = self.client.post("/api/exams/", {"title": "U", "max_attempts": 0}, format="json")
        self.assertEqual(ok.status_code, 201, ok.content)
        bad = self.client.post("/api/exams/", {"title": "V", "max_attempts": 51}, format="json")
        self.assertEqual(bad.status_code, 400)


class FilterChoiceTests(EngineBase):
    def test_choices_only_include_visible_tests(self):
        hidden = self.make_exam(title="Hidden", access_type="assigned")  # nobody assigned
        other_subject = Subject.objects.create(name="Secret")
        q = Question.objects.create(text="secret q", subject=other_subject)
        ExamQuestion.objects.create(section=hidden.sections.first(), question=q, order=9, marks=1, negative_marks=0, question_version=1)
        from exams.models import ExamCategory
        ExamCategory.objects.create(name="Hidden category")
        hidden.category = ExamCategory.objects.get(name="Hidden category")
        hidden.save()
        self.login()
        d = self.client.get("/api/student/filters/").data
        self.assertEqual([s["name"] for s in d["subjects"]], ["Geo"])
        self.assertEqual(d["categories"], [])
