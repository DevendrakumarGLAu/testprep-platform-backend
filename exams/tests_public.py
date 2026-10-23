from rest_framework.test import APITestCase

from accounts.models import Role, User
from questionbank.models import Question, QuestionOption, Subject

from .models import Exam, ExamCategory, ExamQuestion, ExamSection


class PublicHomeTests(APITestCase):
    def setUp(self):
        self.subject = Subject.objects.create(name="Maths")
        self.ssc = ExamCategory.objects.create(name="SSC", description="Staff Selection Commission")
        self.ssc.subjects.add(self.subject)
        self.empty = ExamCategory.objects.create(name="Banking")
        User.objects.create_user(username="s1", email="s1@x.com", password="x", role=Role.STUDENT)
        User.objects.create_user(username="a1", email="a1@x.com", password="x", role=Role.ADMIN)

    def exam(self, title, status="published", access="public", category=None, n=2):
        e = Exam.objects.create(title=title, duration_minutes=10, status=status, access_type=access, category=category or self.ssc)
        sec = ExamSection.objects.create(exam=e, name="S", order=0)
        for i in range(n):
            q = Question.objects.create(text=f"{title} {i}", subject=self.subject)
            QuestionOption.objects.create(question=q, text="a", is_correct=True, order=0)
            QuestionOption.objects.create(question=q, text="b", is_correct=False, order=1)
            ExamQuestion.objects.create(section=sec, question=q, order=i, marks=1, negative_marks=0, question_version=1)
        return e

    def get(self):
        return self.client.get("/api/public/home/")

    def test_anyone_can_read_it(self):
        r = self.get()
        self.assertEqual(r.status_code, 200)
        self.assertIn("max-age", r["Cache-Control"])

    def test_only_open_published_tests_count(self):
        self.exam("Open 1")
        self.exam("Open 2")
        self.exam("Draft", status="draft")
        self.exam("Gone", status="archived")
        self.exam("Private", access="assigned")
        r = self.get()
        self.assertEqual(r.data["stats"], {"tests": 2, "questions": 4, "students": 1})
        self.assertEqual(r.data["exams"], [{"id": self.ssc.public_id, "name": "SSC", "description": "Staff Selection Commission", "tests": 2, "subjects": ["Maths"]}])

    def test_exams_without_an_open_test_are_not_listed(self):
        self.exam("Draft only", status="draft")
        self.assertEqual(self.get().data["exams"], [])

    def test_shows_nothing_private(self):
        self.exam("Open")
        text = str(self.get().data)
        for secret in ("public_id", "a1@x.com", "s1@x.com", "username"):
            self.assertNotIn(secret, text)

    def test_few_queries(self):
        for i in range(5):
            self.exam(f"E{i}", category=ExamCategory.objects.create(name=f"Cat{i}"))
        with self.assertNumQueries(5):
            self.get()
