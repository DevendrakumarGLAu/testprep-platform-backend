import datetime

from django.utils import timezone

from . import activity
from .models import AttemptQuestion
from .tests import EngineBase, user


def days(*offsets, today=None):
    today = today or datetime.date(2026, 10, 23)
    return {today - datetime.timedelta(days=o) for o in offsets}


class StreakMathTests(EngineBase):
    TODAY = datetime.date(2026, 10, 23)

    def test_current_and_best_streak(self):
        self.assertEqual(activity.streaks(days(0, 1, 2), self.TODAY), (3, 3))
        self.assertEqual(activity.streaks(days(0, 1, 2, 5, 6, 7, 8), self.TODAY), (3, 4))

    def test_the_streak_survives_until_the_day_after_a_missed_one(self):
        self.assertEqual(activity.streaks(days(1, 2, 3), self.TODAY), (3, 3))  # nothing yet today, still alive
        self.assertEqual(activity.streaks(days(2, 3, 4), self.TODAY), (0, 3))  # a whole day missed

    def test_no_activity(self):
        self.assertEqual(activity.streaks(set(), self.TODAY), (0, 0))


class ActivityApiTests(EngineBase):
    def setUp(self):
        super().setUp()
        self.login()

    def answer_some(self, n):
        a = self.start()
        qs = self.questions(a)[:n]
        for q in qs:
            self.answer(a, q["id"], self.correct_option(q["id"]))
        return a

    def test_dashboard_reports_todays_questions_and_streak(self):
        self.answer_some(2)
        r = self.client.get("/api/student/dashboard/")
        act = r.data["activity"]
        self.assertEqual(act["today"], 2)
        self.assertEqual(act["streak"], 1)
        self.assertEqual(act["goal"], 20)
        self.assertFalse(act["goal_reached"])
        self.assertEqual(len(act["last_7_days"]), 7)
        self.assertEqual(act["last_7_days"][-1]["questions"], 2)

    def test_yesterday_counts_towards_the_streak(self):
        self.answer_some(1)
        AttemptQuestion.objects.update(answered_at=timezone.now() - datetime.timedelta(days=1))
        act = self.client.get("/api/student/dashboard/").data["activity"]
        self.assertEqual((act["today"], act["streak"]), (0, 1))

    def test_daily_goal_can_be_changed_in_the_profile(self):
        r = self.client.patch("/api/auth/me/", {"daily_goal_questions": 5}, format="json")
        self.assertEqual(r.status_code, 200, r.content)
        self.assertEqual(r.data["daily_goal_questions"], 5)
        self.answer_some(3)
        self.assertEqual(self.client.get("/api/student/dashboard/").data["activity"]["goal"], 5)
        self.assertEqual(self.client.patch("/api/auth/me/", {"daily_goal_questions": 2}, format="json").status_code, 400)
        self.assertEqual(self.client.patch("/api/auth/me/", {"daily_goal_questions": 1000}, format="json").status_code, 400)

    def test_goal_reached(self):
        self.client.patch("/api/auth/me/", {"daily_goal_questions": 5}, format="json")
        a = self.start()
        for q in self.questions(a):
            self.answer(a, q["id"], self.correct_option(q["id"]))
        self.assertFalse(self.client.get("/api/student/dashboard/").data["activity"]["goal_reached"])  # 3 of 5
        b = self.start(self.make_exam(n=3))
        for q in self.questions(b):
            self.answer(b, q["id"], self.correct_option(q["id"]))
        self.assertTrue(self.client.get("/api/student/dashboard/").data["activity"]["goal_reached"])


class LeaderboardTests(EngineBase):
    def attempt_as(self, who, right, exam=None):
        self.client.force_authenticate(who)
        a = self.start(exam)
        for i, q in enumerate(self.questions(a)):
            self.answer(a, q["id"], self.correct_option(q["id"]) if i < right else self.wrong_option(q["id"]))
        self.client.post(f"/api/student/attempts/{a}/submit/")
        return a

    def setUp(self):
        super().setUp()
        self.exam = self.make_exam(n=4, max_attempts=0)
        self.third = user("third")
        self.third.first_name, self.third.last_name = "Tara", "Singh"
        self.third.save()

    def board(self, who):
        self.client.force_authenticate(who)
        return self.client.get(f"/api/student/exams/{self.exam.public_id}/leaderboard/")

    def test_ranks_by_best_attempt_with_a_percentile(self):
        self.attempt_as(self.stu, 1, self.exam)
        self.attempt_as(self.stu, 4, self.exam)  # the second attempt is the best one
        self.attempt_as(self.other, 2, self.exam)
        self.attempt_as(self.third, 0, self.exam)
        r = self.board(self.stu)
        self.assertEqual(r.status_code, 200, r.content)
        self.assertEqual(r.data["participants"], 3)
        self.assertEqual([x["rank"] for x in r.data["top"]], [1, 2, 3])
        self.assertTrue(r.data["top"][0]["is_me"])
        self.assertEqual(r.data["me"]["rank"], 1)
        self.assertEqual(r.data["me"]["better_than_percent"], 66.7)
        self.assertEqual(self.board(self.third).data["me"]["rank"], 3)

    def test_names_are_shortened(self):
        self.attempt_as(self.third, 3, self.exam)
        self.assertEqual(self.board(self.third).data["top"][0]["name"], "Tara S.")

    def test_a_student_who_has_not_attempted_sees_the_board_without_a_rank(self):
        self.attempt_as(self.other, 3, self.exam)
        r = self.board(self.stu)
        self.assertEqual(r.status_code, 200)
        self.assertIsNone(r.data["me"])
        self.assertEqual(r.data["participants"], 1)

    def test_nothing_is_shown_until_results_are_released(self):
        from exams.models import Exam
        Exam.objects.filter(pk=self.exam.pk).update(result_release=Exam.ResultRelease.MANUAL, results_released=False)
        self.assertEqual(self.board(self.stu).status_code, 403)

    def test_unknown_or_hidden_tests_are_not_found(self):
        self.client.force_authenticate(self.stu)
        self.assertEqual(self.client.get("/api/student/exams/nope/leaderboard/").status_code, 404)

    def test_ties_share_a_rank(self):
        self.attempt_as(self.stu, 2, self.exam)
        self.attempt_as(self.other, 2, self.exam)
        ranks = [x["rank"] for x in self.board(self.stu).data["top"]]
        self.assertEqual(ranks[0], 1)
        self.assertIn(ranks[1], (1, 2))  # equal scores share a rank only when the times also match
