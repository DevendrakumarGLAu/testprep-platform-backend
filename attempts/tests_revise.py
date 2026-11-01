from exams.models import Exam, ExamCategory, ExamGroup

from .models import SavedQuestion
from .tests import EngineBase


class ReviseTests(EngineBase):
    def setUp(self):
        super().setUp()
        self.login()

    def take(self, right, exam=None):
        a = self.start(exam)
        for i, q in enumerate(self.questions(a)):
            if i < right:
                self.answer(a, q["id"], self.correct_option(q["id"]))
            elif i == right:
                self.answer(a, q["id"], self.wrong_option(q["id"]))  # the rest are left unanswered
        self.assertEqual(self.client.post(f"/api/student/attempts/{a}/submit/").status_code, 200)
        return a

    def get(self, **params):
        return self.client.get("/api/student/revise/", params)

    def test_mistakes_include_wrong_and_skipped_questions_with_the_answer(self):
        self.take(right=1)  # 1 right, 1 wrong, 1 skipped
        r = self.get(source="mistakes")
        self.assertEqual(r.status_code, 200, r.content)
        self.assertEqual(r.data["total"], 2)
        card = r.data["cards"][0]
        self.assertEqual(sum(o["is_correct"] for o in card["options"]), 1)
        self.assertEqual(card["explanation"], "because")
        self.assertEqual(card["exam_title"], "Mock")

    def test_a_question_answered_right_later_is_no_longer_a_mistake(self):
        self.take(right=0)
        self.assertEqual(self.get(source="mistakes").data["total"], 3)
        # the same questions, all right this time
        a = self.start()
        for q in self.questions(a):
            self.answer(a, q["id"], self.correct_option(q["id"]))
        self.client.post(f"/api/student/attempts/{a}/submit/")
        self.assertEqual(self.get(source="mistakes").data["total"], 0)

    def test_the_same_question_is_one_card_however_often_it_was_missed(self):
        self.take(right=0)
        self.take(right=0)
        self.assertEqual(self.get(source="mistakes").data["total"], 3)

    def test_tests_whose_solutions_are_hidden_are_left_out(self):
        exam = self.make_exam(solution_release=Exam.SolutionRelease.MANUAL, solutions_released=False)
        self.take(right=0, exam=exam)
        self.assertEqual(self.get(source="mistakes").data["total"], 0)

    def test_saved_questions_become_cards(self):
        a = self.take(right=0)
        self.client.post(f"/api/student/attempts/{a}/questions/1/save/", {"note": "n"}, format="json")
        r = self.get(source="saved")
        self.assertEqual((r.data["total"], len(r.data["cards"])), (1, 1))
        self.assertEqual(r.data["cards"][0]["key"], f"s{SavedQuestion.objects.get().id}")

    def test_the_limit_applies_but_total_is_the_whole_pool(self):
        self.take(right=0)
        r = self.get(source="mistakes", limit=2)
        self.assertEqual((r.data["total"], len(r.data["cards"])), (3, 2))
        self.assertEqual(len(self.get(source="mistakes", limit=0).data["cards"]), 1)  # at least one

    def test_bad_input_and_other_peoples_cards(self):
        self.assertEqual(self.get(source="nope").status_code, 400)
        self.assertEqual(self.get(limit="x").status_code, 400)
        self.take(right=0)
        self.client.force_authenticate(self.other)
        self.assertEqual(self.get(source="mistakes").data["total"], 0)
        self.client.force_authenticate(None)
        self.assertEqual(self.get().status_code, 401)


class ReviseByExamTests(ReviseTests):
    def setUp(self):
        super().setUp()
        self.family = ExamGroup.objects.create(name="SSC Exams", order=1)
        self.cgl = ExamCategory.objects.create(name="SSC CGL", group=self.family)
        self.mts = ExamCategory.objects.create(name="SSC MTS", group=self.family)
        self.bank = ExamCategory.objects.create(name="Banking")

    def exam_in(self, category, **over):
        return self.make_exam(category=category, **over)

    def test_the_answer_lists_each_exam_the_student_has_cards_for_with_counts(self):
        self.take(right=0, exam=self.exam_in(self.cgl))
        self.take(right=1, exam=self.exam_in(self.bank))
        exams = self.get(source="mistakes").data["exams"]
        rows = {e["name"]: (e["count"], e["group"], e["id"]) for e in exams}
        self.assertEqual(rows["SSC CGL"], (3, "SSC Exams", self.cgl.public_id))
        self.assertEqual(rows["Banking"], (2, None, self.bank.public_id))
        self.assertNotIn("SSC MTS", rows)  # no cards from it
        self.assertEqual([e["name"] for e in exams], ["SSC CGL", "Banking"])  # families first, then the rest

    def test_filtering_by_exam_keeps_only_its_cards_and_the_total_follows(self):
        self.take(right=0, exam=self.exam_in(self.cgl))
        self.take(right=1, exam=self.exam_in(self.bank))
        r = self.get(source="mistakes", exam=self.cgl.public_id)
        self.assertEqual((r.data["total"], len(r.data["cards"])), (3, 3))
        self.assertEqual({c["exam_title"] for c in r.data["cards"]}, {"Mock"})
        self.assertEqual(self.get(source="mistakes", exam=self.bank.public_id).data["total"], 2)
        self.assertEqual(self.get(source="mistakes", exam=self.mts.public_id).data["total"], 0)
        self.assertEqual(self.get(source="mistakes").data["total"], 5)  # no filter: everything
        self.assertEqual(len(self.get(source="mistakes", exam=self.cgl.public_id).data["exams"]), 2)  # the list does not shrink

    def test_a_test_shared_into_other_exams_counts_for_each(self):
        e = self.exam_in(self.cgl)
        e.also_in.add(self.mts)
        self.take(right=0, exam=e)
        names = {x["name"]: x["count"] for x in self.get(source="mistakes").data["exams"]}
        self.assertEqual(names, {"SSC CGL": 3, "SSC MTS": 3})
        self.assertEqual(self.get(source="mistakes", exam=self.mts.public_id).data["total"], 3)

    def test_tests_without_an_exam_are_other_tests(self):
        self.take(right=0)  # the base exam has no category
        exams = self.get(source="mistakes").data["exams"]
        self.assertEqual([(e["id"], e["name"], e["count"]) for e in exams], [("none", "Other tests", 3)])
        self.assertEqual(self.get(source="mistakes", exam="none").data["total"], 3)
        self.assertEqual(self.get(source="mistakes", exam=self.cgl.public_id).data["total"], 0)

    def test_saved_questions_filter_by_exam_too(self):
        a = self.take(right=0, exam=self.exam_in(self.cgl))
        self.client.post(f"/api/student/attempts/{a}/questions/1/save/", {}, format="json")
        b = self.take(right=0, exam=self.exam_in(self.bank))
        self.client.post(f"/api/student/attempts/{b}/questions/1/save/", {}, format="json")
        r = self.get(source="saved")
        self.assertEqual({e["name"]: e["count"] for e in r.data["exams"]}, {"SSC CGL": 1, "Banking": 1})
        self.assertEqual(self.get(source="saved", exam=self.cgl.public_id).data["total"], 1)

    def test_cards_do_not_leak_the_helper_field(self):
        self.take(right=0, exam=self.exam_in(self.cgl))
        for c in self.get(source="mistakes").data["cards"]:
            self.assertNotIn("_exams", c)
