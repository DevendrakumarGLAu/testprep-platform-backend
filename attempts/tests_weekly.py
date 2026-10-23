import datetime

from django.core import mail
from django.test import override_settings
from django.utils import timezone

from accounts.models import User
from exams.models import Exam
from notifications.models import Notification
from notifications.services import send_weekly_summaries
from questionbank.models import Subject

from . import weekly
from .models import Attempt, AttemptQuestion
from .tests_practice import PracticeBase


class WeeklyBase(PracticeBase):
    def setUp(self):
        super().setUp()
        self.history = Subject.objects.create(name="History")
        self.q_alg = self.topic_questions(self.algebra, 4, "alg")
        self.q_his = self.topic_questions(self.algebra, 4, "his")
        for q in self.q_his:
            Question = type(q)
            Question.objects.filter(pk=q.pk).update(subject=self.history, topic=None)

    def take_both(self, alg_right, his_right):
        self.take(self.exam_with(self.q_alg, "Alg"), right=alg_right)
        self.take(self.exam_with(Question_fresh(self.q_his), "His"), right=his_right)

    def age(self, attempt_pid, days):
        """Moves an attempt (and its answers) back in time."""
        a = Attempt.objects.get(public_id=attempt_pid)
        then = timezone.now() - datetime.timedelta(days=days)
        Attempt.objects.filter(pk=a.pk).update(submitted_at=then)
        AttemptQuestion.objects.filter(attempt=a).update(answered_at=then)


def Question_fresh(qs):
    return [type(q).objects.get(pk=q.pk) for q in qs]


class WeeklySummaryTests(WeeklyBase):
    def test_nothing_done_means_zeros(self):
        r = self.client.get("/api/student/weekly-summary/")
        self.assertEqual(r.status_code, 200)
        self.assertEqual((r.data["questions"], r.data["tests"], r.data["active_days"]), (0, 0, 0))
        self.assertIsNone(r.data["accuracy"])
        self.assertIsNone(r.data["best_subject"])

    def test_this_week_is_counted_with_best_and_weakest_subject(self):
        self.take_both(alg_right=4, his_right=1)
        d = self.client.get("/api/student/weekly-summary/").data
        self.assertEqual((d["questions"], d["tests"], d["active_days"]), (8, 2, 1))
        self.assertEqual(d["accuracy"], 62.5)
        self.assertEqual(d["best_subject"]["accuracy"], 100.0)
        self.assertEqual(d["weakest_subject"]["accuracy"], 25.0)

    def test_the_week_before_is_the_comparison(self):
        a = self.take(self.exam_with(self.q_alg, "Old"), right=2)
        self.age(a, 9)
        self.take(self.exam_with(Question_fresh(self.q_alg), "New"), right=4)
        d = self.client.get("/api/student/weekly-summary/").data
        self.assertEqual((d["questions"], d["questions_before"], d["tests"], d["tests_before"]), (4, 4, 1, 1))
        self.assertEqual((d["accuracy"], d["accuracy_before"], d["accuracy_change"]), (100.0, 50.0, 50.0))

    def test_older_attempts_do_not_count(self):
        a = self.take(self.exam_with(self.q_alg, "Ancient"), right=4)
        self.age(a, 30)
        self.assertEqual(self.client.get("/api/student/weekly-summary/").data["questions"], 0)

    def test_unreleased_results_do_not_show_an_accuracy(self):
        exam = self.exam_with(self.q_alg, "Hidden")
        Exam.objects.filter(pk=exam.pk).update(result_release=Exam.ResultRelease.MANUAL, results_released=False)
        self.take(Exam.objects.get(pk=exam.pk), right=4)
        d = self.client.get("/api/student/weekly-summary/").data
        self.assertEqual(d["questions"], 4)
        self.assertIsNone(d["accuracy"])
        self.assertEqual(d["tests"], 0)

    def test_only_my_own_week_counts_and_only_students_can_ask(self):
        self.take(self.exam_with(self.q_alg, "Mine"), right=4)
        self.client.force_authenticate(self.other)
        self.assertEqual(self.client.get("/api/student/weekly-summary/").data["questions"], 0)
        self.client.force_authenticate(None)
        self.assertEqual(self.client.get("/api/student/weekly-summary/").status_code, 401)


class SendWeeklyTests(WeeklyBase):
    def test_active_students_get_a_note_once_a_week(self):
        self.take(self.exam_with(self.q_alg, "T"), right=3)
        r = send_weekly_summaries()
        self.assertEqual(r["notified"], 1)
        n = Notification.objects.get(user=self.stu, kind=Notification.Kind.WEEKLY)
        self.assertIn("answered 4 questions", n.body)
        self.assertEqual(send_weekly_summaries()["notified"], 0)
        self.assertEqual(Notification.objects.filter(kind=Notification.Kind.WEEKLY).count(), 1)

    def test_students_who_did_nothing_or_opted_out_get_nothing(self):
        self.assertEqual(send_weekly_summaries()["notified"], 0)
        self.take(self.exam_with(self.q_alg, "T"), right=3)
        User.objects.filter(pk=self.stu.pk).update(weekly_summary=False)
        self.assertEqual(send_weekly_summaries()["notified"], 0)

    @override_settings(EMAIL_ENABLED=True, EMAIL_BACKEND="django.core.mail.backends.locmem.EmailBackend", FRONTEND_URL="https://app.example.com")
    def test_it_is_also_emailed_when_email_is_set_up(self):
        self.take(self.exam_with(self.q_alg, "T"), right=3)
        self.assertEqual(send_weekly_summaries(), {"notified": 1, "emailed": 1})
        self.assertEqual(mail.outbox[0].to, [self.stu.email])
        self.assertIn("https://app.example.com/student/dashboard", mail.outbox[0].body)

    def test_the_opt_out_can_be_changed_from_the_profile(self):
        r = self.client.patch("/api/auth/me/", {"weekly_summary": False}, format="json")
        self.assertEqual(r.status_code, 200, r.content)
        self.assertFalse(User.objects.get(pk=self.stu.pk).weekly_summary)


class MessageTests(WeeklyBase):
    def test_message_wording(self):
        data = dict(weekly.summary(self.stu), questions=1, active_days=1, tests=0, questions_before=0, accuracy=None, accuracy_change=None)
        title, body = weekly.message(data)
        self.assertEqual(body, "You answered 1 question on 1 day.")
