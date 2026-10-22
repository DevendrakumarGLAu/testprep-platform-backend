from decimal import Decimal

from django.db import connection
from django.test.utils import CaptureQueriesContext
from rest_framework.test import APITestCase

from accounts.models import Role, User
from attempts.models import AttemptQuestion
from questionbank.models import Question, QuestionOption, Subject, Tag

from .models import Exam

PW = "Str0ng!pass99"
SUBJECTS = ["Maths", "GK", "Reasoning", "English"]


def user(name, role):
    return User.objects.create_user(username=name, email=f"{name}@x.com", password=PW, role=role)


class LargeTestBase(APITestCase):
    PER_SUBJECT = 40

    def setUp(self):
        self.admin = user("adm", Role.ADMIN)
        self.stu = user("stu", Role.STUDENT)
        self.subjects = {n: Subject.objects.create(name=n) for n in SUBJECTS}
        self.by_subject = {}
        for name, subject in self.subjects.items():
            qs = Question.objects.bulk_create(
                Question(
                    text=f"{name} question number {i}", text_hash=f"{name}-{i}", subject=subject, marks=2, negative_marks="0.5",
                    difficulty=("easy", "medium", "hard")[i % 3],
                )
                for i in range(self.PER_SUBJECT)
            )
            QuestionOption.objects.bulk_create(
                [QuestionOption(question=q, text="right", is_correct=True, order=0) for q in qs]
                + [QuestionOption(question=q, text="wrong", is_correct=False, order=1) for q in qs]
            )
            self.by_subject[name] = qs
        self.client.force_authenticate(self.admin)

    def make_test(self, sections: int, per_section: int, **body):
        """A test of sections x per_section questions, drawn evenly from the four subjects."""
        r = self.client.post("/api/exams/", {"title": f"Test {sections * per_section}", "duration_minutes": 120, **body}, format="json")
        self.assertEqual(r.status_code, 201, r.content)
        eid = r.data["id"]
        payload = []
        for s in range(sections):
            name = SUBJECTS[s % 4]
            offset = (s // 4) * per_section
            payload.append({"name": f"{name} {s + 1}", "questions": [{"question": q.id} for q in self.by_subject[name][offset:offset + per_section]]})
        r = self.client.put(f"/api/exams/{eid}/structure/", {"sections": payload}, format="json")
        self.assertEqual(r.status_code, 200, r.content)
        return eid, r.data


class PickEndpointTests(LargeTestBase):
    pick = lambda self, **b: self.client.post("/api/questions/pick/", b, format="json")  # noqa: E731

    def test_random_pick_respects_filters_and_count(self):
        r = self.pick(count=25, mode="random", subject=self.subjects["Maths"].id)
        self.assertEqual(r.status_code, 200, r.content)
        self.assertEqual(r.data["available"], 40)
        ids = [q["id"] for q in r.data["questions"]]
        self.assertEqual(len(ids), 25)
        self.assertEqual(len(set(ids)), 25)
        self.assertEqual({q["subject_name"] for q in r.data["questions"]}, {"Maths"})

    def test_asking_for_more_than_exists_returns_what_exists(self):
        r = self.pick(count=100, mode="random", subject=self.subjects["GK"].id)
        self.assertEqual((r.data["available"], len(r.data["questions"])), (40, 40))

    def test_excluded_questions_are_never_returned(self):
        taken = [q.id for q in self.by_subject["Maths"][:30]]
        r = self.pick(count=40, mode="random", subject=self.subjects["Maths"].id, exclude_ids=taken)
        self.assertEqual(r.data["available"], 10)
        self.assertFalse(set(taken) & {q["id"] for q in r.data["questions"]})

    def test_newest_mode_returns_the_most_recent_first(self):
        r = self.pick(count=5, mode="newest", subject=self.subjects["English"].id)
        newest = sorted((q.id for q in self.by_subject["English"]), reverse=True)[:5]
        self.assertEqual([q["id"] for q in r.data["questions"]], newest)

    def test_inactive_questions_are_never_picked(self):
        Question.objects.filter(subject=self.subjects["Maths"]).update(is_active=False)
        self.assertEqual(self.pick(count=10, subject=self.subjects["Maths"].id).data["available"], 0)

    def test_difficulty_tag_and_search_filters(self):
        r = self.pick(count=300, difficulty="hard", subject=self.subjects["Maths"].id)
        self.assertEqual({q["difficulty"] for q in r.data["questions"]}, {"hard"})
        tag = Tag.objects.create(name="pyq")
        for q in self.by_subject["GK"][:3]:
            q.tags.add(tag)
        self.assertEqual(self.pick(count=10, tag="PYQ").data["available"], 3)
        self.assertEqual(self.pick(count=10, search="number 7").data["available"], 4)  # one per subject

    def test_validation_and_permissions(self):
        self.assertEqual(self.pick(count=0).status_code, 400)
        self.assertEqual(self.pick(count=301).status_code, 400)
        self.assertEqual(self.pick().status_code, 400)
        self.assertEqual(self.pick(count=5, mode="chaos").status_code, 400)
        self.assertEqual(self.pick(count=5, subject="abc").status_code, 400)
        self.assertEqual(self.pick(count=5, exclude_ids="1,2").status_code, 400)
        self.client.force_authenticate(self.stu)
        self.assertEqual(self.pick(count=5).status_code, 403)

    def test_picked_questions_can_go_straight_into_a_test(self):
        picked = self.pick(count=30, mode="random", subject=self.subjects["Reasoning"].id).data["questions"]
        r = self.client.post("/api/exams/", {"title": "Auto", "duration_minutes": 30}, format="json")
        r = self.client.put(f"/api/exams/{r.data['id']}/structure/",
                            {"sections": [{"name": "Reasoning", "questions": [{"question": q["id"]} for q in picked]}]}, format="json")
        self.assertEqual((r.data["question_count"], r.data["total_marks"]), (30, "60.00"))


class BigTestTests(LargeTestBase):
    def test_tests_of_60_100_and_120_questions(self):
        for sections, per in ((2, 30), (4, 25), (4, 30)):
            eid, data = self.make_test(sections, per)
            total = sections * per
            self.assertEqual((data["question_count"], data["total_marks"]), (total, f"{total * 2}.00"), total)
            self.assertEqual(len(data["sections"]), sections)
            listed = next(e for e in self.client.get("/api/exams/?page_size=50").data["results"] if e["id"] == eid)
            self.assertEqual(listed["question_count"], total)

    def test_the_limit_is_300_questions(self):
        r = self.client.post("/api/exams/", {"title": "Huge", "duration_minutes": 300}, format="json")
        eid = r.data["id"]
        ids = [q.id for qs in self.by_subject.values() for q in qs]  # 160 distinct questions
        r = self.client.put(f"/api/exams/{eid}/structure/", {"sections": [{"name": "All", "questions": [{"question": i} for i in ids]}]}, format="json")
        self.assertEqual(r.status_code, 200)
        extra = Question.objects.bulk_create(Question(text=f"Extra {i}", text_hash=f"x{i}", subject=self.subjects["GK"]) for i in range(141))
        QuestionOption.objects.bulk_create([QuestionOption(question=q, text="a", is_correct=True) for q in extra]
                                           + [QuestionOption(question=q, text="b", is_correct=False) for q in extra])
        too_many = ids + [q.id for q in extra]  # 301
        r = self.client.put(f"/api/exams/{eid}/structure/", {"sections": [{"name": "All", "questions": [{"question": i} for i in too_many]}]}, format="json")
        self.assertEqual(r.status_code, 400)
        self.assertIn("at most 300", str(r.data))
        ok = self.client.put(f"/api/exams/{eid}/structure/", {"sections": [{"name": "All", "questions": [{"question": i} for i in too_many[:300]]}]}, format="json")
        self.assertEqual(ok.status_code, 200)

    def test_a_student_can_take_a_120_question_test_start_to_finish(self):
        eid, _ = self.make_test(4, 30, negative_mode="fraction", negative_value="0.25", max_attempts=0)
        self.assertEqual(self.client.post(f"/api/exams/{eid}/publish/").status_code, 200)
        exam = Exam.objects.get(pk=eid)

        self.client.force_authenticate(self.stu)
        with CaptureQueriesContext(connection) as start_q:
            aid = self.client.post(f"/api/student/exams/{exam.public_id}/start/").data["attempt_id"]
        self.assertLess(len(start_q), 25, f"{len(start_q)} queries to start a 120 question test")

        with CaptureQueriesContext(connection) as load_q:
            attempt = self.client.get(f"/api/student/attempts/{aid}/").data
        self.assertLess(len(load_q), 10)
        questions = [q for s in attempt["sections"] for q in s["questions"]]
        self.assertEqual([q["number"] for q in questions], list(range(1, 121)))
        self.assertEqual({q["negative_marks"] for q in questions}, {"0.50"})  # 1/4 of 2

        rows = {aq.id: aq for aq in AttemptQuestion.objects.filter(attempt__public_id=aid)}
        answers = []
        for i, q in enumerate(questions):
            correct = next(o["id"] for o in rows[q["id"]].options if o["is_correct"])
            wrong = next(o["id"] for o in rows[q["id"]].options if not o["is_correct"])
            # 80 right, 20 wrong, 20 skipped
            answers.append({"question": q["id"], "selected_option": correct if i < 80 else wrong if i < 100 else None, "time_spent": 0})
        with CaptureQueriesContext(connection) as save_q:
            r = self.client.put(f"/api/student/attempts/{aid}/answers/", {"answers": answers}, format="json")
        self.assertEqual((r.status_code, r.data["saved"]), (200, 120), r.content)
        self.assertLess(len(save_q), 12, f"{len(save_q)} queries to save 120 answers")

        # A single answer later is also cheap even with 120 rows in the attempt.
        with CaptureQueriesContext(connection) as one_q:
            self.client.put(f"/api/student/attempts/{aid}/answers/", {"question": questions[0]["id"], "marked_for_review": True}, format="json")
        self.assertLess(len(one_q), 10)

        done = self.client.post(f"/api/student/attempts/{aid}/submit/").data["result"]
        # 80 x 2 = 160, minus 20 wrong x 0.5 = 10
        self.assertEqual((done["correct"], done["incorrect"], done["unanswered"]), (80, 20, 20))
        self.assertEqual(done["score"], "150.00")
        self.assertEqual(done["total_marks"], "240.00")

        with CaptureQueriesContext(connection) as result_q:
            review = self.client.get(f"/api/student/attempts/{aid}/result/").data
        self.assertEqual(len(review["questions"]), 120)
        self.assertEqual([s["name"] for s in review["sections"]], [f"{n} {i + 1}" for i, n in enumerate(SUBJECTS)])
        self.assertEqual(sum(Decimal(s["marks"]) for s in review["sections"]), Decimal("150.00"))
        self.assertLess(len(result_q), 14, f"{len(result_q)} queries to build a 120 question result")
