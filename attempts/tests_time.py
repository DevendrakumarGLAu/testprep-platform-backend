from datetime import timedelta

from django.utils import timezone

from exams.models import Exam

from .models import Attempt, AttemptQuestion
from .tests import EngineBase


class TimePerQuestionTests(EngineBase):
    """The exam screen reports a running total of seconds per question; the server keeps it honest."""

    def setUp(self):
        super().setUp()
        self.login()
        self.a = self.start()
        self.qs = self.questions(self.a)
        # Pretend the attempt has been running for two minutes, so there is time to account for.
        Attempt.objects.filter(public_id=self.a).update(started_at=timezone.now() - timedelta(seconds=120))

    def send(self, *items):
        return self.client.put(f"/api/student/attempts/{self.a}/answers/", {"answers": list(items)}, format="json")

    def stored(self, index):
        return AttemptQuestion.objects.get(pk=self.qs[index]["id"]).time_spent_seconds

    def test_time_is_saved_and_comes_back_on_resume(self):
        r = self.send({"question": self.qs[0]["id"], "time_spent": 30}, {"question": self.qs[1]["id"], "time_spent": 45})
        self.assertEqual(r.status_code, 200, r.content)
        self.assertEqual((self.stored(0), self.stored(1), self.stored(2)), (30, 45, 0))
        resumed = self.questions(self.a)
        self.assertEqual([q["time_spent"] for q in resumed], [30, 45, 0])

    def test_running_totals_only_ever_grow(self):
        q = self.qs[0]["id"]
        self.send({"question": q, "time_spent": 50})
        self.send({"question": q, "time_spent": 20})  # a late, stale retry must not lower it
        self.assertEqual(self.stored(0), 50)
        self.send({"question": q, "time_spent": 50})  # resending is harmless
        self.assertEqual(self.stored(0), 50)
        self.send({"question": q, "time_spent": 70})
        self.assertEqual(self.stored(0), 70)

    def test_time_is_saved_alongside_an_answer(self):
        q = self.qs[0]
        self.send({"question": q["id"], "selected_option": self.correct_option(q["id"]), "time_spent": 12})
        self.assertEqual(self.stored(0), 12)
        self.assertIsNotNone(AttemptQuestion.objects.get(pk=q["id"]).selected_option_id)

    def test_cannot_claim_more_time_than_the_attempt_has_been_running(self):
        self.send({"question": self.qs[0]["id"], "time_spent": 5000})
        self.assertLessEqual(self.stored(0), 125)  # 120 s elapsed + the small network allowance
        self.assertGreaterEqual(self.stored(0), 120)

    def test_total_across_questions_is_capped_too(self):
        self.send({"question": self.qs[0]["id"], "time_spent": 100})
        self.send({"question": self.qs[1]["id"], "time_spent": 100})
        total = sum(self.stored(i) for i in range(3))
        self.assertLessEqual(total, 125)
        self.assertEqual(self.stored(0), 100)  # earlier honest time is never taken away

    def test_invalid_values_rejected(self):
        q = self.qs[0]["id"]
        for bad in (-1, "lots", 86401, 1.5e9):
            self.assertEqual(self.send({"question": q, "time_spent": bad}).status_code, 400, bad)
        self.assertEqual(self.stored(0), 0)

    def test_another_student_cannot_add_time(self):
        self.login(self.other)
        r = self.client.put(f"/api/student/attempts/{self.a}/answers/", {"question": self.qs[0]["id"], "time_spent": 10}, format="json")
        self.assertEqual(r.status_code, 404)
        self.assertEqual(self.stored(0), 0)

    def test_no_time_after_submission(self):
        self.client.post(f"/api/student/attempts/{self.a}/submit/")
        self.assertEqual(self.send({"question": self.qs[0]["id"], "time_spent": 10}).status_code, 409)
        self.assertEqual(self.stored(0), 0)

    def test_result_shows_time_per_question_section_and_overall(self):
        self.send({"question": self.qs[0]["id"], "time_spent": 20}, {"question": self.qs[1]["id"], "time_spent": 70},
                  {"question": self.qs[2]["id"], "time_spent": 10})
        self.client.post(f"/api/student/attempts/{self.a}/submit/")
        d = self.client.get(f"/api/student/attempts/{self.a}/result/").data
        self.assertEqual([q["time_spent_seconds"] for q in d["questions"]], [20, 70, 10])
        self.assertEqual(d["sections"][0]["time_seconds"], 100)
        self.assertEqual(d["timing"], {"total_seconds": 100, "average_seconds": 33, "slowest": {"number": 2, "seconds": 70}})

    def test_time_is_visible_even_when_solutions_are_still_hidden(self):
        Exam.objects.filter(pk=self.exam.pk).update(solution_release="manual")
        self.send({"question": self.qs[0]["id"], "time_spent": 15})
        self.client.post(f"/api/student/attempts/{self.a}/submit/")
        d = self.client.get(f"/api/student/attempts/{self.a}/result/").data
        self.assertFalse(d["solutions_available"])
        self.assertEqual(d["questions"][0]["time_spent_seconds"], 15)

    def test_result_without_any_timing_data(self):
        self.client.post(f"/api/student/attempts/{self.a}/submit/")
        d = self.client.get(f"/api/student/attempts/{self.a}/result/").data
        self.assertEqual(d["timing"], {"total_seconds": 0, "average_seconds": 0, "slowest": None})

    def test_admin_sees_the_same_and_average_time_per_question(self):
        self.send({"question": self.qs[0]["id"], "time_spent": 40})
        self.client.post(f"/api/student/attempts/{self.a}/submit/")
        self.client.force_authenticate(self.admin)
        attempt = Attempt.objects.get(public_id=self.a)
        review = self.client.get(f"/api/attempts/{attempt.pk}/").data
        self.assertEqual(review["questions"][0]["time_spent_seconds"], 40)
        summary = self.client.get(f"/api/exams/{self.exam.pk}/results-summary/").data
        by_time = sorted(summary["questions"], key=lambda q: -q["average_seconds"])
        self.assertEqual(by_time[0]["average_seconds"], 40)
