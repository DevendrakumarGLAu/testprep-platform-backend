from datetime import timedelta

from django.utils import timezone

from exams.models import Exam

from .models import Attempt
from .services import expire_overdue
from .tests import EngineBase
from .tests_sectional import SectionalBase


class PauseTests(EngineBase):
    def setUp(self):
        super().setUp()
        self.login()
        self.exam = self.make_exam(title="Pausable", max_attempts=0, max_pauses=3, pause_minutes=10)  # a test that sets limits
        self.aid = self.start()

    def pause(self):
        return self.client.post(f"/api/student/attempts/{self.aid}/pause/")

    def resume(self):
        return self.client.post(f"/api/student/attempts/{self.aid}/resume/")

    def attempt(self):
        return Attempt.objects.get(public_id=self.aid)

    def shift_pause(self, seconds):
        """Pretend the pause began `seconds` ago: the whole timeline of the attempt moves back by that much."""
        a = self.attempt()
        shift = timedelta(seconds=seconds)
        Attempt.objects.filter(pk=a.pk).update(paused_at=a.paused_at - shift, started_at=a.started_at - shift, deadline=a.deadline - shift)

    def test_attempt_freezes_the_exam_pause_rules(self):
        a = self.attempt()
        self.assertTrue(a.allow_pause)
        self.assertEqual((a.max_pauses, a.pause_minutes), (3, 10))

    def test_pause_hides_the_test_and_stops_the_clock(self):
        before = self.client.get(f"/api/student/attempts/{self.aid}/").data["remaining_seconds"]
        r = self.pause()
        self.assertEqual(r.status_code, 200, r.content)
        self.assertTrue(r.data["paused"])
        self.assertEqual(r.data["sections"], [])
        self.assertTrue(r.data["pause"]["paused"])
        self.shift_pause(120)  # two minutes later, the clock must not have moved
        d = self.client.get(f"/api/student/attempts/{self.aid}/").data
        self.assertTrue(d["paused"])
        self.assertLessEqual(abs(d["remaining_seconds"] - before), 3)
        self.assertNotIn("questions", str(d["sections"]))

    def test_resume_gives_back_the_same_time_and_the_questions(self):
        self.pause()
        self.shift_pause(120)
        before = self.attempt().deadline
        r = self.resume()
        self.assertEqual(r.status_code, 200, r.content)
        self.assertFalse(r.data.get("paused", False))
        self.assertEqual(len([q for s in r.data["sections"] for q in s["questions"]]), 3)
        a = self.attempt()
        self.assertIsNone(a.paused_at)
        self.assertAlmostEqual((a.deadline - before).total_seconds(), 120, delta=3)  # deadline moved by the pause
        self.assertAlmostEqual(a.paused_seconds, 120, delta=3)
        self.assertAlmostEqual(r.data["remaining_seconds"], 30 * 60, delta=8)

    def test_answers_are_refused_while_paused(self):
        q = self.questions(self.aid)[0]
        self.pause()
        r = self.client.put(f"/api/student/attempts/{self.aid}/answers/", {"question": q["id"], "selected_option": q["options"][0]["id"]}, format="json")
        self.assertEqual(r.status_code, 409)
        self.assertTrue(r.data["paused"])

    def test_pause_is_idempotent_and_counts_once(self):
        self.pause()
        self.assertEqual(self.pause().status_code, 200)
        self.assertEqual(self.attempt().pause_count, 1)

    def test_pause_limit(self):
        for _ in range(3):
            self.assertEqual(self.pause().status_code, 200)
            self.assertEqual(self.resume().status_code, 200)
        r = self.pause()
        self.assertEqual(r.status_code, 409)
        self.assertIn("used all 3 pauses", r.data["detail"])

    def test_unlimited_pauses_when_the_limit_is_zero(self):
        Attempt.objects.filter(public_id=self.aid).update(max_pauses=0)
        for _ in range(5):
            self.assertEqual(self.pause().status_code, 200)
            self.assertEqual(self.resume().status_code, 200)
        self.assertIsNone(self.client.get(f"/api/student/attempts/{self.aid}/").data["pause"]["pauses_left"])

    def test_pause_off_for_the_test(self):
        exam = self.make_exam(title="No pause", max_attempts=0, allow_pause=False)
        aid = self.start(exam)
        r = self.client.post(f"/api/student/attempts/{aid}/pause/")
        self.assertEqual(r.status_code, 409)
        self.assertIn("not allowed", r.data["detail"])

    def test_a_pause_that_runs_over_ends_by_itself_and_the_clock_runs_again(self):
        self.pause()
        self.shift_pause(12 * 60)  # 12 minutes paused, the limit is 10: the last 2 minutes count against the student
        d = self.client.get(f"/api/student/attempts/{self.aid}/").data
        self.assertFalse(d.get("paused", False))
        self.assertEqual(len([q for s in d["sections"] for q in s["questions"]]), 3)
        self.assertAlmostEqual(d["remaining_seconds"], 28 * 60, delta=8)
        self.assertAlmostEqual(self.attempt().paused_seconds, 10 * 60, delta=3)

    def test_an_abandoned_pause_cannot_hold_the_test_open(self):
        self.pause()
        self.shift_pause(3 * 3600)  # the student never came back
        d = self.client.get(f"/api/student/attempts/{self.aid}/").data
        self.assertEqual(d["status"], "submitted")
        self.assertTrue(self.attempt().auto_submitted)

    def test_the_scheduler_does_not_submit_a_paused_attempt(self):
        self.pause()
        Attempt.objects.filter(public_id=self.aid).update(deadline=timezone.now() - timedelta(minutes=5))
        expire_overdue()
        self.assertEqual(self.attempt().status, "in_progress")

    def test_submitting_while_paused_resumes_and_scores(self):
        q = self.questions(self.aid)[0]
        self.client.put(f"/api/student/attempts/{self.aid}/answers/", {"question": q["id"], "selected_option": self.correct_option(q["id"])}, format="json")
        self.pause()
        self.shift_pause(60)
        r = self.client.post(f"/api/student/attempts/{self.aid}/submit/")
        self.assertEqual(r.status_code, 200, r.content)
        a = self.attempt()
        self.assertEqual(a.status, "submitted")
        self.assertIsNone(a.paused_at)
        self.assertFalse(a.auto_submitted)
        self.assertEqual(a.correct_count, 1)

    def test_paused_time_is_not_part_of_the_time_taken(self):
        self.pause()
        self.shift_pause(300)
        self.resume()
        a = self.attempt()
        Attempt.objects.filter(pk=a.pk).update(started_at=a.started_at - timedelta(seconds=100))  # 400 seconds in all, 300 of them paused
        self.client.post(f"/api/student/attempts/{self.aid}/submit/")
        taken = self.attempt().time_taken_seconds
        self.assertAlmostEqual(taken, 400 - 300, delta=6)

    def test_cannot_pause_after_time_is_up(self):
        Attempt.objects.filter(public_id=self.aid).update(deadline=timezone.now() - timedelta(seconds=30))
        r = self.pause()
        self.assertEqual(r.status_code, 409)
        self.assertEqual(self.attempt().status, "submitted")

    def test_window_end_still_caps_the_resumed_deadline(self):
        end = timezone.now() + timedelta(minutes=31)
        Exam.objects.filter(pk=self.exam.pk).update(end_at=end)
        self.pause()
        self.shift_pause(8 * 60)
        self.resume()
        self.assertLessEqual(self.attempt().deadline, end + timedelta(seconds=1))

    def test_another_student_cannot_pause_it(self):
        self.client.force_authenticate(self.other)
        self.assertEqual(self.pause().status_code, 404)

    def test_resume_on_a_submitted_attempt_is_refused(self):
        self.client.post(f"/api/student/attempts/{self.aid}/submit/")
        self.assertEqual(self.resume().status_code, 409)
        self.assertEqual(self.pause().status_code, 409)


class SectionalPauseTests(SectionalBase):
    def test_section_clock_stops_and_continues(self):
        self.start()
        self.client.post(f"/api/student/attempts/{self.aid}/pause/")
        a = Attempt.objects.get(public_id=self.aid)
        Attempt.objects.filter(pk=a.pk).update(paused_at=a.paused_at - timedelta(minutes=9))  # a 9 minute pause
        # still paused: Maths (10 minutes) must not have expired
        d = self.state()
        self.assertTrue(d["paused"])
        r = self.client.post(f"/api/student/attempts/{self.aid}/resume/")
        self.assertEqual(r.status_code, 200)
        maths = next(s for s in r.data["sections"] if s["name"] == "Maths")
        self.assertEqual(maths["status"], "active")
        self.assertGreaterEqual(maths["remaining_seconds"], 10 * 60 - 8)

    def test_cannot_submit_a_section_while_paused(self):
        self.start()
        self.client.post(f"/api/student/attempts/{self.aid}/pause/")
        self.assertEqual(self.submit_section(0).status_code, 409)


class ResultLinkTests(EngineBase):
    """A student who has taken a test gets the id of their latest result on the test card."""

    def setUp(self):
        super().setUp()
        self.login()

    def card(self, exam=None):
        r = self.client.get("/api/student/exams/")
        self.assertEqual(r.status_code, 200)
        return next(c for c in r.data if c["id"] == (exam or self.exam).public_id)

    def finish(self, exam=None):
        aid = self.start(exam)
        self.client.post(f"/api/student/attempts/{aid}/submit/")
        return aid

    def test_untaken_test_has_no_result_link(self):
        c = self.card()
        self.assertIsNone(c["last_attempt_id"])
        self.assertFalse(c["result_available"])

    def test_taken_test_links_to_the_latest_attempt(self):
        exam = self.make_exam(title="Retake", max_attempts=0)
        first = self.finish(exam)
        second = self.finish(exam)
        c = self.card(exam)
        self.assertEqual(c["last_attempt_id"], second)
        self.assertNotEqual(c["last_attempt_id"], first)
        self.assertTrue(c["result_available"])

    def test_link_is_marked_unavailable_until_results_are_released(self):
        exam = self.make_exam(title="Held", max_attempts=0, result_release=Exam.ResultRelease.MANUAL)
        aid = self.finish(exam)
        c = self.card(exam)
        self.assertEqual(c["last_attempt_id"], aid)
        self.assertFalse(c["result_available"])

    def test_a_running_attempt_is_not_a_result(self):
        self.start()
        self.assertIsNone(self.card()["last_attempt_id"])

    def test_students_only_see_their_own_attempts(self):
        aid = self.finish()
        self.client.force_authenticate(self.other)
        self.assertIsNone(self.card()["last_attempt_id"])
        self.client.force_authenticate(self.stu)
        self.assertEqual(self.card()["last_attempt_id"], aid)

    def test_detail_page_carries_it_too(self):
        aid = self.finish()
        r = self.client.get(f"/api/student/exams/{self.exam.public_id}/")
        self.assertEqual(r.data["last_attempt_id"], aid)
        self.assertTrue(r.data["result_available"])


class UnlimitedPauseTests(EngineBase):
    """The default: a student may pause as often as they like and stay paused as long as they like."""

    def setUp(self):
        super().setUp()
        self.login()
        self.exam = self.make_exam(title="Free pause", max_attempts=0)
        self.aid = self.start()
        self.get = lambda: self.client.get(f"/api/student/attempts/{self.aid}/").data

    def pause(self):
        return self.client.post(f"/api/student/attempts/{self.aid}/pause/")

    def resume(self):
        return self.client.post(f"/api/student/attempts/{self.aid}/resume/")

    def back_date(self, hours):
        a = Attempt.objects.get(public_id=self.aid)
        shift = timedelta(hours=hours)
        Attempt.objects.filter(pk=a.pk).update(paused_at=a.paused_at - shift, started_at=a.started_at - shift, deadline=a.deadline - shift)

    def test_a_new_test_has_no_pause_limits(self):
        e = Exam.objects.get(pk=self.exam.pk)
        self.assertEqual((e.max_pauses, e.pause_minutes), (0, 0))
        a = Attempt.objects.get(public_id=self.aid)
        self.assertEqual((a.max_pauses, a.pause_minutes), (0, 0))

    def test_the_pause_says_it_never_ends_by_itself(self):
        r = self.pause()
        self.assertIsNone(r.data["pause"]["auto_resume_at"])
        self.assertIsNone(r.data["pause"]["pauses_left"])
        self.assertEqual(r.data["pause"]["max_minutes"], 0)

    def test_a_student_can_stay_paused_for_days_and_resume_with_the_same_time_left(self):
        before = self.get()["remaining_seconds"]
        self.pause()
        self.back_date(hours=72)
        d = self.get()
        self.assertTrue(d["paused"])  # nothing resumed it
        self.assertEqual(d["status"], "in_progress")
        self.assertLessEqual(abs(d["remaining_seconds"] - before), 3)
        r = self.resume()
        self.assertEqual(r.status_code, 200, r.content)
        after = self.get()
        self.assertFalse(after.get("paused", False))
        self.assertLessEqual(abs(after["remaining_seconds"] - before), 3)

    def test_pausing_again_and_again_is_always_allowed(self):
        for _ in range(8):
            self.assertEqual(self.pause().status_code, 200)
            self.assertEqual(self.resume().status_code, 200)
        self.assertEqual(Attempt.objects.get(public_id=self.aid).pause_count, 8)
