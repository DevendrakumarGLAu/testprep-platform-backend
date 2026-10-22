from datetime import timedelta

from django.utils import timezone
from rest_framework.test import APITestCase

from accounts.models import Role, User
from exams.models import Exam, ExamQuestion, ExamSection
from questionbank.models import Question, QuestionOption, Subject

from .models import Attempt, AttemptQuestion, AttemptSection

PW = "Str0ng!pass99"
SECTIONS = [("Maths", 10, 2), ("GK", 5, 2), ("English", 5, 1)]  # name, minutes, questions


def user(name, role=Role.STUDENT):
    return User.objects.create_user(username=name, email=f"{name}@x.com", password=PW, role=role)


class SectionalBase(APITestCase):
    def setUp(self):
        self.admin = user("adm", Role.ADMIN)
        self.stu = user("stu")
        self.other = user("other")
        self.subject = Subject.objects.create(name="Mixed")
        self.exam = self.make_exam()
        self.client.force_authenticate(self.stu)

    def make_exam(self, **over):
        exam = Exam.objects.create(
            title="SSC Full", status="published", sectional_timing=True, duration_minutes=sum(m for _, m, _ in SECTIONS),
            max_attempts=0, **over,
        )
        for si, (name, minutes, n) in enumerate(SECTIONS):
            sec = ExamSection.objects.create(exam=exam, name=name, order=si, duration_minutes=minutes)
            for i in range(n):
                q = Question.objects.create(text=f"{name} question {i + 1}", subject=self.subject)
                QuestionOption.objects.create(question=q, text="right", is_correct=True, order=0)
                QuestionOption.objects.create(question=q, text="wrong", is_correct=False, order=1)
                ExamQuestion.objects.create(section=sec, question=q, order=i, marks=2, negative_marks=0, question_version=1)
        return exam

    def start(self):
        r = self.client.post(f"/api/student/exams/{self.exam.public_id}/start/")
        self.assertEqual(r.status_code, 201, r.content)
        self.aid = r.data["attempt_id"]
        return self.aid

    def state(self):
        return self.client.get(f"/api/student/attempts/{self.aid}/").data

    def rewind(self, seconds):
        """Pretend the attempt started `seconds` earlier (all clocks are derived from the start time)."""
        a = Attempt.objects.get(public_id=self.aid)
        shift = timedelta(seconds=seconds)
        Attempt.objects.filter(pk=a.pk).update(started_at=a.started_at - shift, deadline=a.deadline - shift)
        first = AttemptSection.objects.filter(attempt=a).order_by("order").first()
        AttemptSection.objects.filter(pk=first.pk).update(started_at=first.started_at - shift)
        # sections already started by a manual submit moved with it
        for st in AttemptSection.objects.filter(attempt=a).exclude(pk=first.pk):
            AttemptSection.objects.filter(pk=st.pk).update(
                started_at=st.started_at - shift if st.started_at else None, ended_at=st.ended_at - shift if st.ended_at else None
            )
        AttemptSection.objects.filter(pk=first.pk, ended_at__isnull=False).update(ended_at=first.ended_at - shift if first.ended_at else None)

    def aqs(self, section_order):
        return list(AttemptQuestion.objects.filter(attempt__public_id=self.aid, section_order=section_order).order_by("order"))

    def answer(self, aq, correct=True, **extra):
        opt = next(o["id"] for o in aq.options if o["is_correct"] == correct)
        return self.client.put(f"/api/student/attempts/{self.aid}/answers/",
                               {"question": aq.id, "selected_option": opt, **extra}, format="json")

    def submit_section(self, order):
        return self.client.post(f"/api/student/attempts/{self.aid}/sections/submit/", {"order": order}, format="json")

    def sec(self, data, name):
        return next(s for s in data["sections"] if s["name"] == name)


class OpeningTests(SectionalBase):
    def test_only_the_first_section_is_open_and_later_ones_are_hidden(self):
        self.start()
        d = self.state()
        self.assertTrue(d["sectional"])
        self.assertEqual([(s["name"], s["status"]) for s in d["sections"]], [("Maths", "active"), ("GK", "upcoming"), ("English", "upcoming")])
        self.assertEqual([len(s["questions"]) for s in d["sections"]], [2, 0, 0])
        self.assertEqual([s["question_count"] for s in d["sections"]], [2, 2, 1])
        self.assertLessEqual(self.sec(d, "Maths")["remaining_seconds"], 600)
        self.assertGreaterEqual(self.sec(d, "Maths")["remaining_seconds"], 595)
        self.assertEqual(d["active_section"], 0)
        body = str(d)
        self.assertIn("Maths question 1", body)
        self.assertNotIn("GK question", body)
        self.assertNotIn("English question", body)
        self.assertNotIn("is_correct", body)

    def test_total_time_is_the_sum_of_the_sections(self):
        self.start()
        self.assertAlmostEqual(self.state()["remaining_seconds"], 20 * 60, delta=5)

    def test_numbering_continues_across_sections(self):
        self.start()
        self.submit_section(0)
        self.assertEqual([q["number"] for q in self.sec(self.state(), "GK")["questions"]], [3, 4])

    def test_ordinary_tests_are_unaffected(self):
        Exam.objects.filter(pk=self.exam.pk).update(sectional_timing=False)
        self.start()
        d = self.state()
        self.assertFalse(d["sectional"])
        self.assertEqual([len(s["questions"]) for s in d["sections"]], [2, 2, 1])
        self.assertEqual(self.submit_section(0).status_code, 400)


class LockedOrderTests(SectionalBase):
    def test_answers_in_a_section_that_has_not_opened_are_ignored(self):
        self.start()
        gk = self.aqs(1)[0]
        r = self.answer(gk, time_spent=5)
        self.assertEqual((r.status_code, r.data["saved"], r.data["ignored"]), (200, 0, 1))
        gk.refresh_from_db()
        self.assertIsNone(gk.selected_option_id)
        self.assertEqual(gk.time_spent_seconds, 0)

    def test_the_open_section_accepts_answers(self):
        self.start()
        r = self.answer(self.aqs(0)[0])
        self.assertEqual((r.data["saved"], r.data["ignored"]), (1, 0))

    def test_a_mixed_batch_saves_what_is_allowed_and_ignores_the_rest(self):
        self.start()
        items = [{"question": self.aqs(0)[0].id, "selected_option": self.aqs(0)[0].options[0]["id"]},
                 {"question": self.aqs(2)[0].id, "selected_option": self.aqs(2)[0].options[0]["id"]}]
        r = self.client.put(f"/api/student/attempts/{self.aid}/answers/", {"answers": items}, format="json")
        self.assertEqual((r.data["saved"], r.data["ignored"]), (1, 1))

    def test_finished_sections_cannot_be_reopened_or_changed(self):
        self.start()
        maths = self.aqs(0)[0]
        self.answer(maths)
        self.submit_section(0)
        r = self.answer(maths, correct=False)
        self.assertEqual(r.data["ignored"], 1)
        maths.refresh_from_db()
        self.assertTrue(maths.selected_option_id is not None and next(o for o in maths.options if o["id"] == maths.selected_option_id)["is_correct"])
        d = self.state()
        self.assertEqual(self.sec(d, "Maths")["status"], "done")
        self.assertEqual(self.sec(d, "Maths")["questions"], [])  # nothing to go back to
        self.assertEqual(self.sec(d, "Maths")["answered_count"], 1)

    def test_other_students_cannot_touch_the_attempt(self):
        self.start()
        self.client.force_authenticate(self.other)
        self.assertEqual(self.submit_section(0).status_code, 404)


class SubmittingASectionTests(SectionalBase):
    def test_submitting_a_section_opens_the_next_one_with_its_own_clock(self):
        self.start()
        r = self.submit_section(0)
        self.assertEqual(r.status_code, 200, r.content)
        self.assertTrue(r.data["moved"])
        self.assertEqual([(s["name"], s["status"]) for s in r.data["sections"]], [("Maths", "done"), ("GK", "active"), ("English", "upcoming")])
        self.assertEqual(self.sec(r.data, "Maths")["closed_by"], "submitted")
        gk = self.sec(r.data, "GK")
        self.assertEqual(len(gk["questions"]), 2)
        self.assertLessEqual(gk["remaining_seconds"], 300)
        self.assertGreaterEqual(gk["remaining_seconds"], 295)

    def test_finishing_early_shortens_the_overall_deadline(self):
        self.start()
        self.submit_section(0)  # 10 minutes saved
        self.assertLessEqual(self.state()["remaining_seconds"], 10 * 60 + 5)
        self.assertGreaterEqual(self.state()["remaining_seconds"], 10 * 60 - 5)

    def test_a_double_click_does_not_skip_a_section(self):
        self.start()
        self.assertTrue(self.submit_section(0).data["moved"])
        again = self.submit_section(0)
        self.assertFalse(again.data["moved"])
        self.assertEqual(self.sec(again.data, "GK")["status"], "active")
        self.assertEqual(self.sec(again.data, "English")["status"], "upcoming")

    def test_cannot_jump_ahead_by_submitting_a_later_section(self):
        self.start()
        r = self.submit_section(2)
        self.assertFalse(r.data["moved"])
        self.assertEqual(self.sec(r.data, "Maths")["status"], "active")

    def test_bad_requests(self):
        self.start()
        self.assertEqual(self.client.post(f"/api/student/attempts/{self.aid}/sections/submit/", {}, format="json").status_code, 400)
        self.assertEqual(self.client.post(f"/api/student/attempts/{self.aid}/sections/submit/", {"order": "x"}, format="json").status_code, 400)

    def test_submitting_the_last_section_submits_the_test_and_scores_every_section(self):
        self.start()
        self.answer(self.aqs(0)[0])
        self.answer(self.aqs(0)[1], correct=False)
        self.submit_section(0)
        self.answer(self.aqs(1)[0])
        self.submit_section(1)
        self.answer(self.aqs(2)[0])
        r = self.submit_section(2)
        self.assertEqual((r.data["status"], r.data["moved"]), ("submitted", True))
        self.assertEqual(r.data["result"]["score"], "6.00")  # 3 correct x 2 marks, no negative marking
        self.assertEqual((r.data["result"]["correct"], r.data["result"]["incorrect"], r.data["result"]["unanswered"]), (3, 1, 1))

    def test_whole_test_submit_is_refused_until_the_last_section(self):
        self.start()
        r = self.client.post(f"/api/student/attempts/{self.aid}/submit/")
        self.assertEqual(r.status_code, 409)
        self.assertIn("current section", r.data["detail"])
        self.assertEqual(Attempt.objects.get(public_id=self.aid).status, "in_progress")
        self.submit_section(0)
        self.submit_section(1)
        done = self.client.post(f"/api/student/attempts/{self.aid}/submit/")
        self.assertEqual(done.status_code, 200)
        self.assertEqual(done.data["status"], "submitted")


class SectionTimerTests(SectionalBase):
    def test_a_section_closes_by_itself_when_its_time_runs_out(self):
        self.start()
        self.answer(self.aqs(0)[0])
        self.rewind(11 * 60)  # 1 minute into GK
        d = self.state()
        self.assertEqual([(s["name"], s["status"]) for s in d["sections"]], [("Maths", "done"), ("GK", "active"), ("English", "upcoming")])
        self.assertEqual(self.sec(d, "Maths")["closed_by"], "expired")
        self.assertEqual(self.sec(d, "Maths")["questions"], [])
        gk = self.sec(d, "GK")
        self.assertAlmostEqual(gk["remaining_seconds"], 4 * 60, delta=5)
        self.assertEqual(len(gk["questions"]), 2)

    def test_the_next_section_starts_when_the_previous_one_ended_not_when_the_student_looked(self):
        self.start()
        self.rewind(14 * 60)  # nobody looked for 14 minutes: Maths ended at 10:00, GK has 1 minute left
        self.assertAlmostEqual(self.sec(self.state(), "GK")["remaining_seconds"], 60, delta=5)

    def test_several_sections_can_expire_between_two_visits(self):
        self.start()
        self.rewind(16 * 60)
        d = self.state()
        self.assertEqual([s["status"] for s in d["sections"]], ["done", "done", "active"])
        self.assertAlmostEqual(self.sec(d, "English")["remaining_seconds"], 4 * 60, delta=5)

    def test_reading_the_state_repeatedly_is_stable(self):
        self.start()
        self.rewind(11 * 60)
        first = [(s["status"], s.get("remaining_seconds", 0) // 10) for s in self.state()["sections"]]
        second = [(s["status"], s.get("remaining_seconds", 0) // 10) for s in self.state()["sections"]]
        self.assertEqual(first, second)
        self.assertEqual(AttemptSection.objects.filter(attempt__public_id=self.aid, ended_at__isnull=False).count(), 1)

    def test_a_save_just_after_the_timer_is_still_accepted_but_not_a_late_one(self):
        self.start()
        maths = self.aqs(0)[0]
        self.rewind(10 * 60 + 2)  # Maths ended 2 seconds ago
        self.assertEqual(self.answer(maths).data["saved"], 1)
        self.rewind(60)  # now a minute late
        r = self.answer(self.aqs(0)[1])
        self.assertEqual((r.data["saved"], r.data["ignored"]), (0, 1))

    def test_the_whole_test_ends_when_the_last_section_runs_out(self):
        self.start()
        self.answer(self.aqs(0)[0])
        self.rewind(21 * 60)
        d = self.state()
        self.assertEqual(d["status"], "submitted")
        self.assertTrue(d["auto_submitted"])
        self.assertEqual(d["result"]["correct"], 1)

    def test_saving_after_everything_expired_reports_the_attempt_as_submitted(self):
        self.start()
        self.rewind(21 * 60)
        r = self.answer(self.aqs(2)[0])
        self.assertEqual(r.status_code, 409)

    def test_the_exam_window_closing_still_caps_the_attempt(self):
        Exam.objects.filter(pk=self.exam.pk).update(end_at=timezone.now() + timedelta(minutes=8))
        self.start()
        self.assertLessEqual(self.state()["remaining_seconds"], 8 * 60 + 2)
        self.assertLessEqual(self.sec(self.state(), "Maths")["remaining_seconds"], 8 * 60 + 2)  # never more than the whole test has left


class AdminSideTests(SectionalBase):
    def setUp(self):
        super().setUp()
        self.client.force_authenticate(self.admin)

    def build(self, **durations):
        r = self.client.post("/api/exams/", {"title": "New", "duration_minutes": 30, "sectional_timing": True}, format="json")
        self.assertEqual(r.status_code, 201, r.content)
        eid = r.data["id"]
        qs = list(Question.objects.all()[:6])
        sections = [
            {"name": "Maths", "duration_minutes": durations.get("a", 20), "questions": [{"question": qs[0].id}, {"question": qs[1].id}]},
            {"name": "GK", "duration_minutes": durations.get("b", 10), "questions": [{"question": qs[2].id}, {"question": qs[3].id}]},
        ]
        r = self.client.put(f"/api/exams/{eid}/structure/", {"sections": sections}, format="json")
        return eid, r

    def test_section_time_limits_are_saved_and_set_the_total_duration(self):
        eid, r = self.build()
        self.assertEqual(r.status_code, 200, r.content)
        self.assertEqual([s["duration_minutes"] for s in r.data["sections"]], [20, 10])
        self.assertEqual(r.data["duration_minutes"], 30)
        self.assertTrue(r.data["sectional_timing"])

    def test_every_section_needs_a_time_limit_to_publish(self):
        eid, _ = self.build(b=None)
        r = self.client.post(f"/api/exams/{eid}/publish/")
        self.assertEqual(r.status_code, 400)
        self.assertTrue(any("GK" in e and "time limit" in e for e in r.data["errors"]), r.data)

    def test_publishing_syncs_the_total_and_locks_the_setting(self):
        eid, _ = self.build()
        self.assertEqual(self.client.post(f"/api/exams/{eid}/publish/").status_code, 200)
        self.assertEqual(Exam.objects.get(pk=eid).duration_minutes, 30)
        r = self.client.patch(f"/api/exams/{eid}/", {"sectional_timing": False}, format="json")
        self.assertEqual(r.status_code, 400)
        self.assertIn("sectional_timing", r.data)

    def test_total_cannot_exceed_600_minutes(self):
        eid, r = self.build(a=400, b=300)
        self.assertEqual(r.status_code, 400)
        self.assertIn("600", str(r.data))

    def test_turning_the_setting_on_syncs_existing_sections(self):
        eid, _ = self.build()
        self.client.patch(f"/api/exams/{eid}/", {"sectional_timing": False, "duration_minutes": 45}, format="json")
        r = self.client.patch(f"/api/exams/{eid}/", {"sectional_timing": True}, format="json")
        self.assertEqual(r.data["duration_minutes"], 30)

    def test_duplicate_and_preview_keep_the_section_times(self):
        eid, _ = self.build()
        copy = self.client.post(f"/api/exams/{eid}/duplicate/").data
        self.assertEqual([s["duration_minutes"] for s in copy["sections"]], [20, 10])
        self.assertTrue(copy["sectional_timing"])
        preview = self.client.get(f"/api/exams/{eid}/preview/").data
        self.assertTrue(preview["sectional_timing"])
        self.assertEqual([s["duration_minutes"] for s in preview["sections"]], [20, 10])

    def test_students_see_the_section_times_before_starting(self):
        self.client.force_authenticate(self.stu)
        d = self.client.get(f"/api/student/exams/{self.exam.public_id}/").data
        self.assertTrue(d["sectional_timing"])
        self.assertEqual([(s["name"], s["duration_minutes"]) for s in d["sections"]], [("Maths", 10), ("GK", 5), ("English", 5)])
        card = next(c for c in self.client.get("/api/student/exams/").data if c["id"] == self.exam.public_id)
        self.assertTrue(card["sectional_timing"])
        self.assertEqual(card["duration_minutes"], 20)

    def test_ordinary_tests_show_no_section_times(self):
        Exam.objects.filter(pk=self.exam.pk).update(sectional_timing=False)
        self.client.force_authenticate(self.stu)
        d = self.client.get(f"/api/student/exams/{self.exam.public_id}/").data
        self.assertEqual({s["duration_minutes"] for s in d["sections"]}, {None})
