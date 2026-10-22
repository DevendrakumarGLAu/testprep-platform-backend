from decimal import Decimal

from rest_framework.test import APITestCase

from accounts.models import Role, User
from attempts.models import AttemptQuestion
from questionbank.models import Question, QuestionOption, Subject

from .models import Exam, ExamQuestion

PW = "Str0ng!pass99"


class NegativeRuleTests(APITestCase):
    """One negative marking rule per test: a fraction of each question's marks, or a fixed amount."""

    def setUp(self):
        self.admin = User.objects.create_user(username="adm", email="a@x.com", password=PW, role=Role.ADMIN)
        self.stu = User.objects.create_user(username="stu", email="s@x.com", password=PW, role=Role.STUDENT)
        subject = Subject.objects.create(name="Maths")
        # The bank questions deliberately disagree about negative marking.
        self.q = []
        for i, (marks, neg) in enumerate([("2", "0.5"), ("2", "0"), ("4", "2")]):
            q = Question.objects.create(text=f"Question {i}", subject=subject, marks=marks, negative_marks=neg)
            QuestionOption.objects.create(question=q, text="right", is_correct=True, order=0)
            QuestionOption.objects.create(question=q, text="wrong", is_correct=False, order=1)
            self.q.append(q)
        self.client.force_authenticate(self.admin)

    def make(self, **body):
        base = {"title": "T", "duration_minutes": 10}
        r = self.client.post("/api/exams/", {**base, **body}, format="json")
        self.assertEqual(r.status_code, 201, r.content)
        return r.data["id"]

    def structure(self, eid, **item):
        sections = [{"name": "S", "questions": [{"question": q.id, **item} for q in self.q]}]
        r = self.client.put(f"/api/exams/{eid}/structure/", {"sections": sections}, format="json")
        self.assertEqual(r.status_code, 200, r.content)
        return list(ExamQuestion.objects.filter(section__exam_id=eid).order_by("order").values_list("marks", "negative_marks"))

    def test_default_uses_each_questions_own_value(self):
        eid = self.make()
        self.assertEqual(self.structure(eid), [(Decimal("2"), Decimal("0.5")), (Decimal("2"), Decimal("0")), (Decimal("4"), Decimal("2"))])

    def test_quarter_of_the_marks(self):
        eid = self.make(negative_mode="fraction", negative_value="0.25")
        self.assertEqual([n for _, n in self.structure(eid)], [Decimal("0.5"), Decimal("0.5"), Decimal("1")])

    def test_one_third_of_the_marks_keeps_four_decimals(self):
        eid = self.make(negative_mode="fraction", negative_value="0.3333")
        self.assertEqual([n for _, n in self.structure(eid)], [Decimal("0.6666"), Decimal("0.6666"), Decimal("1.3332")])

    def test_fixed_amount_for_every_wrong_answer(self):
        eid = self.make(negative_mode="fixed", negative_value="0.45")
        self.assertEqual([n for _, n in self.structure(eid)], [Decimal("0.45")] * 3)

    def test_rule_overrides_values_sent_for_individual_questions(self):
        eid = self.make(negative_mode="fixed", negative_value="0.25")
        self.assertEqual([n for _, n in self.structure(eid, negative_marks="9")], [Decimal("0.25")] * 3)

    def test_custom_marks_per_question_scale_the_fraction(self):
        eid = self.make(negative_mode="fraction", negative_value="0.25")
        self.assertEqual(self.structure(eid, marks="8"), [(Decimal("8"), Decimal("2"))] * 3)

    def test_changing_the_rule_updates_existing_questions(self):
        eid = self.make()
        self.structure(eid)
        r = self.client.patch(f"/api/exams/{eid}/", {"negative_mode": "fraction", "negative_value": "0.5"}, format="json")
        self.assertEqual(r.status_code, 200, r.content)
        self.assertEqual([n for _, n in ExamQuestion.objects.filter(section__exam_id=eid).order_by("order").values_list("marks", "negative_marks")],
                         [Decimal("1"), Decimal("1"), Decimal("2")])
        self.client.patch(f"/api/exams/{eid}/", {"negative_mode": "question"}, format="json")  # back to each question's own
        self.assertEqual([n for _, n in ExamQuestion.objects.filter(section__exam_id=eid).order_by("order").values_list("marks", "negative_marks")],
                         [Decimal("0.5"), Decimal("0"), Decimal("2")])

    def test_editing_something_else_does_not_reset_hand_set_values(self):
        eid = self.make()
        self.structure(eid, negative_marks="0.75")
        self.client.patch(f"/api/exams/{eid}/", {"title": "Renamed"}, format="json")
        self.assertEqual({n for _, n in ExamQuestion.objects.filter(section__exam_id=eid).values_list("marks", "negative_marks")}, {Decimal("0.75")})

    def test_validation(self):
        bad = lambda **b: self.client.post("/api/exams/", {"title": "T", "duration_minutes": 10, **b}, format="json").status_code  # noqa: E731
        self.assertEqual(bad(negative_mode="fraction", negative_value="0"), 400)
        self.assertEqual(bad(negative_mode="fraction", negative_value="1.5"), 400)
        self.assertEqual(bad(negative_mode="fixed", negative_value="-1"), 400)
        self.assertEqual(bad(negative_mode="fixed", negative_value="500"), 400)
        self.assertEqual(bad(negative_mode="sideways"), 400)
        self.assertEqual(bad(negative_mode="fraction", negative_value="1"), 201)

    def test_question_mode_ignores_a_stray_value(self):
        r = self.client.post("/api/exams/", {"title": "T", "duration_minutes": 10, "negative_value": "0.9"}, format="json")
        self.assertEqual(Decimal(r.data["negative_value"]), 0)

    def test_rule_is_locked_once_published_and_duplicate_keeps_it(self):
        eid = self.make(negative_mode="fraction", negative_value="0.25")
        self.structure(eid)
        self.client.post(f"/api/exams/{eid}/publish/")
        r = self.client.patch(f"/api/exams/{eid}/", {"negative_value": "0.5"}, format="json")
        self.assertEqual(r.status_code, 400)
        self.assertIn("negative_value", r.data)
        copy = self.client.post(f"/api/exams/{eid}/duplicate/").data
        self.assertEqual((copy["negative_mode"], Decimal(copy["negative_value"])), ("fraction", Decimal("0.25")))

    def test_scoring_uses_the_rule_and_rounds_only_the_final_score(self):
        eid = self.make(negative_mode="fraction", negative_value="0.3333", result_release="immediate", max_attempts=0)
        self.structure(eid)
        self.client.post(f"/api/exams/{eid}/publish/")
        exam = Exam.objects.get(pk=eid)
        self.client.force_authenticate(self.stu)
        aid = self.client.post(f"/api/student/exams/{exam.public_id}/start/").data["attempt_id"]
        for aq in AttemptQuestion.objects.filter(attempt__public_id=aid):
            wrong = next(o["id"] for o in aq.options if not o["is_correct"])
            self.client.put(f"/api/student/attempts/{aid}/answers/", {"question": aq.id, "selected_option": wrong}, format="json")
        done = self.client.post(f"/api/student/attempts/{aid}/submit/").data
        # marks 2, 2, 4 -> deductions 0.6666 + 0.6666 + 1.3332 = 2.6664
        self.assertEqual(done["result"]["score"], "-2.67")
        review = self.client.get(f"/api/student/attempts/{aid}/result/").data
        self.assertEqual([q["marks_awarded"] for q in review["questions"]], ["-0.67", "-0.67", "-1.33"])
        self.assertEqual(review["sections"][0]["marks"], "-2.67")

    def test_students_see_two_decimals(self):
        eid = self.make(negative_mode="fraction", negative_value="0.3333")
        self.structure(eid)
        self.client.post(f"/api/exams/{eid}/publish/")
        exam = Exam.objects.get(pk=eid)
        self.client.force_authenticate(self.stu)
        aid = self.client.post(f"/api/student/exams/{exam.public_id}/start/").data["attempt_id"]
        qs = self.client.get(f"/api/student/attempts/{aid}/").data["sections"][0]["questions"]
        self.assertEqual([q["negative_marks"] for q in qs], ["0.67", "0.67", "1.33"])
        self.client.force_authenticate(self.admin)
        preview = self.client.get(f"/api/exams/{eid}/preview/").data
        self.assertEqual(preview["sections"][0]["questions"][0]["negative_marks"], "0.67")
