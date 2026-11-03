from attempts.tests import EngineBase, user


class RankTests(EngineBase):
    def setUp(self):
        super().setUp()
        self.third = user("third")

    def take(self, who, right):
        """Answers `right` questions correctly (the rest wrongly) and submits."""
        self.login(who)
        attempt = self.start()
        for i, q in enumerate(self.questions(attempt)):
            self.answer(attempt, q["id"], self.correct_option(q["id"]) if i < right else self.wrong_option(q["id"]))
        self.assertEqual(self.client.post(f"/api/student/attempts/{attempt}/submit/").status_code, 200)

    def board(self, who):
        self.login(who)
        return self.client.get(f"/api/student/exams/{self.exam.public_id}/leaderboard/").data

    def test_rank_and_percentile_after_a_test(self):
        self.take(self.stu, 3)
        self.take(self.other, 2)
        self.take(self.third, 0)
        top, mid, low = self.board(self.stu)["me"], self.board(self.other)["me"], self.board(self.third)["me"]
        self.assertEqual((top["rank"], top["percentile"]), (1, 100.0))
        self.assertEqual((mid["rank"], mid["percentile"]), (2, 66.7))
        self.assertEqual((low["rank"], low["percentile"]), (3, 33.3))
        self.assertEqual(top["better_than_percent"], 66.7)  # the older figure is still there

    def test_a_lone_student_is_at_the_top(self):
        self.take(self.stu, 2)
        me = self.board(self.stu)["me"]
        self.assertEqual((me["rank"], me["percentile"]), (1, 100.0))

    def test_my_ranks_lists_each_test_once(self):
        self.take(self.stu, 3)
        self.take(self.other, 1)
        second = self.make_exam(title="Second mock")
        self.login(self.stu)
        attempt = self.start(second)
        for q in self.questions(attempt):
            self.answer(attempt, q["id"], self.wrong_option(q["id"]))
        self.client.post(f"/api/student/attempts/{attempt}/submit/")
        data = self.client.get("/api/student/ranks/").data
        self.assertEqual(len(data["results"]), 2)
        by_title = {r["exam_title"]: r for r in data["results"]}
        self.assertEqual((by_title["Mock"]["rank"], by_title["Mock"]["participants"], by_title["Mock"]["percentile"]), (1, 2, 100.0))
        self.assertEqual(by_title["Second mock"]["participants"], 1)
        self.assertEqual(data["average_percentile"], 100.0)

    def test_ranks_empty_and_students_only(self):
        self.login()
        self.assertEqual(self.client.get("/api/student/ranks/").data, {"results": [], "average_percentile": None})
        self.client.force_authenticate(self.admin)
        self.assertEqual(self.client.get("/api/student/ranks/").status_code, 403)
