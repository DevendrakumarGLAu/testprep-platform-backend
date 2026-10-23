import datetime

from django.utils import timezone

from exams.models import Exam, ExamQuestion, ExamSection
from questionbank.models import Question, QuestionOption, Topic

from .tests import EngineBase


class PracticeBase(EngineBase):
    def setUp(self):
        super().setUp()
        self.algebra = Topic.objects.create(subject=self.subject, name="Algebra")
        self.rivers = Topic.objects.create(subject=self.subject, name="Rivers")
        self.login()

    def topic_questions(self, topic, n, prefix, date=None):
        out = []
        for i in range(n):
            q = Question.objects.create(text=f"{prefix} {i}", subject=self.subject, topic=topic, explanation="why", question_date=date)
            QuestionOption.objects.create(question=q, text="right", is_correct=True, order=0)
            QuestionOption.objects.create(question=q, text="wrong", is_correct=False, order=1)
            out.append(q)
        return out

    def take(self, exam, right):
        """Attempt the test as the student: the first `right` questions answered correctly, the rest wrongly."""
        a = self.start(exam)
        for i, q in enumerate(self.questions(a)):
            opt = self.correct_option(q["id"]) if i < right else self.wrong_option(q["id"])
            self.answer(a, q["id"], opt)
        self.assertEqual(self.client.post(f"/api/student/attempts/{a}/submit/").status_code, 200)
        return a

    def exam_with(self, questions, title="Topic test"):
        e = Exam.objects.create(title=title, duration_minutes=30, max_attempts=0, status=Exam.Status.PUBLISHED)
        sec = ExamSection.objects.create(exam=e, name="S", order=0)
        for i, q in enumerate(questions):
            ExamQuestion.objects.create(section=sec, question=q, order=i, marks=1, negative_marks=0, question_version=q.version)
        return e


class WeakTopicTests(PracticeBase):
    def test_weakest_topics_are_found_and_strong_ones_are_not(self):
        alg = self.topic_questions(self.algebra, 5, "alg")
        riv = self.topic_questions(self.rivers, 5, "riv")
        self.take(self.exam_with(alg), right=5)
        self.take(self.exam_with(riv, "Rivers test"), right=0)
        r = self.client.get("/api/student/weak-topics/")
        self.assertEqual([t["topic"] for t in r.data["weak"]], ["Rivers"])
        self.assertEqual(r.data["weak"][0]["accuracy"], 0.0)
        self.assertEqual({t["topic"] for t in r.data["all"]}, {"Algebra", "Rivers"})

    def test_a_topic_needs_a_few_answers_before_it_counts(self):
        self.take(self.exam_with(self.topic_questions(self.algebra, 2, "few")), right=0)
        self.assertEqual(self.client.get("/api/student/weak-topics/").data["weak"], [])

    def test_only_my_own_history_counts(self):
        self.take(self.exam_with(self.topic_questions(self.algebra, 4, "mine")), right=0)
        self.client.force_authenticate(self.other)
        self.assertEqual(self.client.get("/api/student/weak-topics/").data["all"], [])


class PracticeSetTests(PracticeBase):
    def test_a_weak_topic_set_starts_an_attempt_on_a_hidden_test(self):
        weak = self.topic_questions(self.rivers, 6, "riv")
        self.take(self.exam_with(weak), right=0)
        self.topic_questions(self.rivers, 4, "new riv")
        r = self.client.post("/api/student/practice/weak/", {"count": 8}, format="json")
        self.assertEqual(r.status_code, 200, r.content)
        exam = Exam.objects.get(public_id=r.data["exam_id"])
        self.assertTrue(exam.is_practice)
        self.assertEqual(exam.owner, self.stu)
        self.assertEqual(r.data["questions"], 8)
        used = set(exam.sections.first().items.values_list("question_id", flat=True))
        self.assertTrue({q.id for q in weak} <= used)  # the ones answered wrong come first
        a = self.client.get(f"/api/student/attempts/{r.data['attempt_id']}/")
        self.assertEqual(a.status_code, 200)
        self.assertEqual(len([q for s in a.data["sections"] for q in s["questions"]]), 8)

    def test_practice_sets_stay_out_of_every_list(self):
        self.take(self.exam_with(self.topic_questions(self.rivers, 5, "riv")), right=0)
        self.client.post("/api/student/practice/weak/", {"count": 5}, format="json")
        titles = lambda r: " ".join(e["title"] for e in (r.data["results"] if isinstance(r.data, dict) else r.data))  # noqa: E731
        self.assertNotIn("Practice", titles(self.client.get("/api/student/exams/")))
        self.client.force_authenticate(self.admin)
        self.assertNotIn("Practice", titles(self.client.get("/api/exams/")))
        self.assertEqual(self.client.get("/api/admin/dashboard/").status_code, 200)

    def test_nobody_else_can_open_my_practice_set(self):
        self.take(self.exam_with(self.topic_questions(self.rivers, 5, "riv")), right=0)
        r = self.client.post("/api/student/practice/weak/", {"count": 5}, format="json")
        self.client.force_authenticate(self.other)
        self.assertEqual(self.client.get(f"/api/student/exams/{r.data['exam_id']}/").status_code, 404)
        self.assertEqual(self.client.post(f"/api/student/exams/{r.data['exam_id']}/start/").status_code, 404)

    def test_no_history_means_a_clear_message(self):
        r = self.client.post("/api/student/practice/weak/", {"count": 5}, format="json")
        self.assertEqual(r.status_code, 404)
        self.assertIn("Take a few tests", r.data["detail"])

    def test_count_is_checked(self):
        for bad in (0, 51, "x"):
            self.assertEqual(self.client.post("/api/student/practice/weak/", {"count": bad}, format="json").status_code, 400)

    def test_redo_mistakes_uses_only_the_wrong_and_skipped_questions(self):
        a = self.start(self.exam_with(self.topic_questions(self.algebra, 6, "alg")))
        questions = self.questions(a)
        self.answer(a, questions[0]["id"], self.correct_option(questions[0]["id"]))
        self.answer(a, questions[1]["id"], self.wrong_option(questions[1]["id"]))
        self.client.post(f"/api/student/attempts/{a}/submit/")
        r = self.client.post(f"/api/student/attempts/{a}/practice-wrong/", {}, format="json")
        self.assertEqual(r.status_code, 200, r.content)
        self.assertEqual(r.data["questions"], 5)
        only_wrong = self.client.post(f"/api/student/attempts/{a}/practice-wrong/", {"include_unanswered": False}, format="json")
        self.assertEqual(only_wrong.data["questions"], 1)

    def test_redo_mistakes_needs_a_submitted_attempt_and_something_to_redo(self):
        a = self.start(self.exam_with(self.topic_questions(self.algebra, 3, "alg")))
        self.assertEqual(self.client.post(f"/api/student/attempts/{a}/practice-wrong/", {}, format="json").status_code, 409)
        for q in self.questions(a):
            self.answer(a, q["id"], self.correct_option(q["id"]))
        self.client.post(f"/api/student/attempts/{a}/submit/")
        self.assertEqual(self.client.post(f"/api/student/attempts/{a}/practice-wrong/", {}, format="json").status_code, 404)

    def test_another_students_attempt_is_not_found(self):
        a = self.take(self.exam_with(self.topic_questions(self.algebra, 3, "alg")), right=0)
        self.client.force_authenticate(self.other)
        self.assertEqual(self.client.post(f"/api/student/attempts/{a}/practice-wrong/", {}, format="json").status_code, 404)


class DailyQuizTests(PracticeBase):
    def recent(self, n, days_ago=3):
        return self.topic_questions(self.algebra, n, "recent", date=timezone.localdate() - datetime.timedelta(days=days_ago))

    def test_not_available_until_enough_recent_questions_exist(self):
        self.recent(4)
        self.assertFalse(self.client.get("/api/student/daily-quiz/").data["available"])
        self.assertEqual(self.client.post("/api/student/daily-quiz/").status_code, 404)

    def test_quiz_uses_recent_questions_only(self):
        recent = self.recent(12)
        self.topic_questions(self.algebra, 5, "old", date=timezone.localdate() - datetime.timedelta(days=400))
        r = self.client.post("/api/student/daily-quiz/")
        self.assertEqual(r.status_code, 200, r.content)
        self.assertEqual(r.data["questions"], 10)
        exam = Exam.objects.get(public_id=r.data["exam_id"])
        used = set(exam.sections.first().items.values_list("question_id", flat=True))
        self.assertTrue(used <= {q.id for q in recent})

    def test_same_quiz_all_day_and_only_one_attempt(self):
        self.recent(12)
        first = self.client.post("/api/student/daily-quiz/")
        again = self.client.post("/api/student/daily-quiz/")
        self.assertEqual(first.data["exam_id"], again.data["exam_id"])
        self.assertEqual(first.data["attempt_id"], again.data["attempt_id"])  # resumes the running attempt
        self.client.post(f"/api/student/attempts/{first.data['attempt_id']}/submit/")
        self.assertTrue(self.client.get("/api/student/daily-quiz/").data["done"])
        self.assertEqual(self.client.post("/api/student/daily-quiz/").status_code, 409)

    def test_each_student_gets_their_own(self):
        self.recent(12)
        mine = self.client.post("/api/student/daily-quiz/").data["exam_id"]
        self.client.force_authenticate(self.other)
        theirs = self.client.post("/api/student/daily-quiz/").data["exam_id"]
        self.assertNotEqual(mine, theirs)
