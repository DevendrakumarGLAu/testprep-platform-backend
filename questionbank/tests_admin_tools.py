import datetime

from django.utils import timezone
from rest_framework.test import APITestCase

from accounts.models import Role, User
from attempts.tests import EngineBase
from exams.models import Exam, ExamQuestion, ExamSection

from .models import Question, QuestionOption, Subject, Topic

PW = "Str0ng!pass99"


class ToolsBase(APITestCase):
    def setUp(self):
        self.admin = User.objects.create_user(username="adm", email="a@x.com", password=PW, role=Role.ADMIN)
        self.student = User.objects.create_user(username="stu", email="s@x.com", password=PW, role=Role.STUDENT)
        self.maths = Subject.objects.create(name="Maths")
        self.gk = Subject.objects.create(name="GK")
        self.client.force_authenticate(self.admin)

    def make(self, text, subject=None, **extra):
        q = Question.objects.create(text=text, subject=subject or self.maths, **extra)
        QuestionOption.objects.create(question=q, text="a", is_correct=True, order=0)
        QuestionOption.objects.create(question=q, text="b", is_correct=False, order=1)
        return q


class BulkEditTests(ToolsBase):
    def bulk(self, ids, action, **extra):
        return self.client.post("/api/questions/bulk/", {"ids": ids, "action": action, **extra}, format="json")

    def test_set_the_question_date_of_many_questions(self):
        qs = [self.make(f"q{i}", question_date=datetime.date(2020, 1, 1)) for i in range(3)]
        r = self.bulk([q.id for q in qs], "set_question_date", question_date="2026-07-08")
        self.assertEqual((r.status_code, r.data["updated"]), (200, 3))
        self.assertEqual({str(Question.objects.get(pk=q.id).question_date) for q in qs}, {"2026-07-08"})

    def test_the_date_cannot_be_in_the_future_or_missing(self):
        q = self.make("q")
        tomorrow = (timezone.localdate() + datetime.timedelta(days=1)).isoformat()
        self.assertEqual(self.bulk([q.id], "set_question_date", question_date=tomorrow).status_code, 400)
        self.assertEqual(self.bulk([q.id], "set_question_date").status_code, 400)

    def test_move_questions_to_another_subject_and_clear_the_topic(self):
        topic = Topic.objects.create(subject=self.maths, name="Algebra")
        q = self.make("q", topic=topic)
        r = self.bulk([q.id], "set_subject", subject=self.gk.id)
        self.assertEqual(r.status_code, 200, r.content)
        q.refresh_from_db()
        self.assertEqual((q.subject_id, q.topic_id), (self.gk.id, None))
        self.assertEqual(self.bulk([q.id], "set_subject").status_code, 400)

    def test_an_archived_subject_cannot_be_chosen(self):
        self.gk.is_active = False
        self.gk.save()
        self.assertEqual(self.bulk([self.make("q").id], "set_subject", subject=self.gk.id).status_code, 400)

    def test_ids_of_everything_matching_the_filters(self):
        for i in range(5):
            self.make(f"maths {i}")
        self.make("gk", self.gk)
        r = self.client.get(f"/api/questions/ids/?subject={self.maths.id}")
        self.assertEqual((r.data["count"], len(r.data["ids"]), r.data["truncated"]), (5, 5, False))
        self.assertEqual(self.client.get("/api/questions/ids/").data["count"], 6)

    def test_ids_are_capped(self):
        Question.objects.bulk_create([Question(text=f"x{i}", text_hash=str(i), subject=self.maths) for i in range(505)])
        r = self.client.get("/api/questions/ids/")
        self.assertEqual((len(r.data["ids"]), r.data["truncated"], r.data["count"]), (500, True, 505))

    def test_students_cannot_use_any_of_it(self):
        self.client.force_authenticate(self.student)
        self.assertEqual(self.client.get("/api/questions/ids/").status_code, 403)
        self.assertEqual(self.client.get("/api/questions/duplicates/").status_code, 403)
        self.assertEqual(self.client.post("/api/questions/duplicates/resolve/", {}, format="json").status_code, 403)


class DuplicateTests(ToolsBase):
    def setUp(self):
        super().setUp()
        self.a = self.make("What is the capital of India?")
        self.b = self.make("What is the capital of India?")
        self.c = self.make("What is the capital of India?")
        self.other = self.make("Something else entirely")

    def used_by(self, exam_title, question):
        exam = Exam.objects.create(title=exam_title, duration_minutes=10)
        sec = ExamSection.objects.create(exam=exam, name="S", order=0)
        ExamQuestion.objects.create(section=sec, question=question, order=0, marks=1, negative_marks=0, question_version=1)

    def test_exact_copies_are_grouped(self):
        r = self.client.get("/api/questions/duplicates/")
        self.assertEqual(len(r.data["groups"]), 1)
        self.assertEqual(sorted(q["id"] for q in r.data["groups"][0]["questions"]), sorted([self.a.id, self.b.id, self.c.id]))

    def test_the_group_shows_where_each_copy_is_used(self):
        self.used_by("Mock A", self.b)
        group = self.client.get("/api/questions/duplicates/").data["groups"][0]
        used = {q["id"]: q["used_in"] for q in group["questions"]}
        self.assertEqual(used[self.b.id], ["Mock A"])
        self.assertEqual(used[self.a.id], [])

    def test_archive_the_extra_copies_and_keep_one(self):
        r = self.client.post("/api/questions/duplicates/resolve/", {"keep": self.a.id, "archive": [self.b.id, self.c.id]}, format="json")
        self.assertEqual((r.status_code, r.data["archived"]), (200, 2))
        active = set(Question.objects.filter(is_active=True).values_list("id", flat=True))
        self.assertEqual(active, {self.a.id, self.other.id})
        self.assertEqual(self.client.get("/api/questions/duplicates/").data["groups"], [])

    def test_reports_tests_that_still_use_an_archived_copy(self):
        self.used_by("Mock A", self.b)
        r = self.client.post("/api/questions/duplicates/resolve/", {"keep": self.a.id, "archive": [self.b.id]}, format="json")
        self.assertEqual(r.data["still_used_in"], ["Mock A"])

    def test_only_true_copies_can_be_archived(self):
        r = self.client.post("/api/questions/duplicates/resolve/", {"keep": self.a.id, "archive": [self.other.id]}, format="json")
        self.assertEqual(r.status_code, 400)
        self.assertTrue(Question.objects.get(pk=self.other.id).is_active)
        for bad in ({}, {"keep": "x", "archive": [1]}, {"keep": self.a.id, "archive": []}):
            self.assertEqual(self.client.post("/api/questions/duplicates/resolve/", bad, format="json").status_code, 400)

    def test_keeping_a_question_never_archives_it(self):
        r = self.client.post("/api/questions/duplicates/resolve/", {"keep": self.a.id, "archive": [self.a.id]}, format="json")
        self.assertEqual(r.status_code, 400)
        self.assertTrue(Question.objects.get(pk=self.a.id).is_active)


class InsightsTests(EngineBase):
    def take(self, who, right, exam=None):
        self.client.force_authenticate(who)
        a = self.start(exam)
        for i, q in enumerate(self.questions(a)):
            if i < right:
                self.answer(a, q["id"], self.correct_option(q["id"]))
            elif i == right:
                self.answer(a, q["id"], self.wrong_option(q["id"]))
            # the rest are skipped
        self.client.post(f"/api/student/attempts/{a}/submit/")

    def setUp(self):
        super().setUp()
        self.exam = self.make_exam(n=4, max_attempts=0)
        self.students = [User.objects.create_user(username=f"s{i}", email=f"s{i}@x.com", password=PW, role=Role.STUDENT) for i in range(5)]

    def test_busiest_tests_hardest_and_most_skipped_questions(self):
        for s in self.students:
            self.take(s, 2, self.exam)  # q1, q2 right; q3 wrong; q4 skipped
        self.client.force_authenticate(self.admin)
        r = self.client.get("/api/admin/insights/")
        self.assertEqual(r.status_code, 200, r.content)
        self.assertEqual(r.data["attempts"], 5)
        self.assertEqual(r.data["tests"][0]["attempts"], 5)
        self.assertEqual(r.data["tests"][0]["average_percent"], 37.5)
        self.assertEqual(r.data["hardest"][0]["correct_percent"], 0.0)
        self.assertEqual(r.data["most_skipped"][0]["skipped_percent"], 100.0)

    def test_questions_need_enough_answers_before_they_are_ranked(self):
        for s in self.students[:2]:
            self.take(s, 1, self.exam)
        self.client.force_authenticate(self.admin)
        r = self.client.get("/api/admin/insights/")
        self.assertEqual((r.data["hardest"], r.data["most_skipped"]), ([], []))

    def test_practice_sets_do_not_count(self):
        self.take(self.stu, 1, self.exam)
        Exam.objects.filter(pk=self.exam.pk).update(is_practice=True)
        self.client.force_authenticate(self.admin)
        self.assertEqual(self.client.get("/api/admin/insights/").data["attempts"], 0)

    def test_dropoff_by_question_number(self):
        for s in self.students:
            self.take(s, 2, self.exam)
        self.client.force_authenticate(self.admin)
        r = self.client.get(f"/api/exams/{self.exam.id}/dropoff/")
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.data["attempts"], 5)
        pos = r.data["positions"]
        self.assertEqual([p["number"] for p in pos], [1, 2, 3, 4])
        self.assertEqual([p["answered_percent"] for p in pos], [100.0, 100.0, 100.0, 0.0])
        self.assertEqual([p["correct_percent"] for p in pos], [100.0, 100.0, 0.0, None])

    def test_students_cannot_see_insights(self):
        self.client.force_authenticate(self.stu)
        self.assertEqual(self.client.get("/api/admin/insights/").status_code, 403)
        self.assertEqual(self.client.get(f"/api/exams/{self.exam.id}/dropoff/").status_code, 403)
