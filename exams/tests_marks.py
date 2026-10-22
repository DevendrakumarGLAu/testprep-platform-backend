from decimal import Decimal

from rest_framework.test import APITestCase

from accounts.models import Role, User
from questionbank.models import Question, QuestionOption, Subject

from .models import ExamQuestion

PW = "Str0ng!pass99"


class MarksPerTestTests(APITestCase):
    """Marks and negative marking belong to the test: the same question is 2 marks in SSC and 1 mark in RRB."""

    def setUp(self):
        self.admin = User.objects.create_user(username="adm", email="a@x.com", password=PW, role=Role.ADMIN)
        self.subject = Subject.objects.create(name="Maths")
        self.client.force_authenticate(self.admin)
        r = self.client.post("/api/questions/", {
            "text": "2+2?", "subject": self.subject.id,
            "options": [{"text": "4", "is_correct": True}, {"text": "5", "is_correct": False}],
        }, format="json")
        self.assertEqual(r.status_code, 201, r.content)  # no marks and no negative marks sent
        self.q = Question.objects.get(pk=r.data["id"])

    def exam(self, **body):
        r = self.client.post("/api/exams/", {"title": "T", "duration_minutes": 10, **body}, format="json")
        self.assertEqual(r.status_code, 201, r.content)
        return r.data["id"]

    def put(self, eid):
        r = self.client.put(f"/api/exams/{eid}/structure/", {"sections": [{"name": "S", "questions": [{"question": self.q.id}]}]}, format="json")
        self.assertEqual(r.status_code, 200, r.content)
        return ExamQuestion.objects.get(section__exam_id=eid)

    def test_a_question_can_be_created_without_marks(self):
        self.assertEqual((self.q.marks, self.q.negative_marks), (Decimal("1"), Decimal("0")))

    def test_ssc_style_two_marks_and_a_quarter_off(self):
        item = self.put(self.exam(marks_per_question="2", negative_mode="fraction", negative_value="0.25"))
        self.assertEqual((item.marks, item.negative_marks), (Decimal("2"), Decimal("0.5")))

    def test_the_same_question_in_an_rrb_test_is_one_mark_and_a_third_off(self):
        item = self.put(self.exam(marks_per_question="1", negative_mode="fraction", negative_value="0.3333"))
        self.assertEqual((item.marks, item.negative_marks), (Decimal("1"), Decimal("0.3333")))

    def test_changing_the_test_marks_updates_its_questions(self):
        eid = self.exam(marks_per_question="1", negative_mode="fraction", negative_value="0.25")
        self.put(eid)
        r = self.client.patch(f"/api/exams/{eid}/", {"marks_per_question": "4"}, format="json")
        self.assertEqual(r.status_code, 200, r.content)
        item = ExamQuestion.objects.get(section__exam_id=eid)
        self.assertEqual((item.marks, item.negative_marks), (Decimal("4"), Decimal("1")))
        self.assertEqual(r.data["total_marks"], "4.00")

    def test_marks_sent_per_question_are_ignored_when_the_test_sets_them(self):
        eid = self.exam(marks_per_question="2")
        r = self.client.put(f"/api/exams/{eid}/structure/", {"sections": [{"name": "S", "questions": [{"question": self.q.id, "marks": "9"}]}]}, format="json")
        self.assertEqual(r.status_code, 200)
        self.assertEqual(ExamQuestion.objects.get(section__exam_id=eid).marks, Decimal("2"))

    def test_must_be_positive(self):
        r = self.client.post("/api/exams/", {"title": "T", "duration_minutes": 10, "marks_per_question": "0"}, format="json")
        self.assertEqual(r.status_code, 400)
        self.assertIn("marks_per_question", r.data)

    def test_locked_once_published(self):
        eid = self.exam(marks_per_question="2")
        self.put(eid)
        self.assertEqual(self.client.post(f"/api/exams/{eid}/publish/").status_code, 200)
        r = self.client.patch(f"/api/exams/{eid}/", {"marks_per_question": "3"}, format="json")
        self.assertEqual(r.status_code, 400, r.content)
        self.assertEqual(ExamQuestion.objects.get(section__exam_id=eid).marks, Decimal("2"))
