from datetime import timedelta

from django.utils import timezone

from exams.models import Exam

from .models import Attempt
from .tests_prep import PrepBase, user


def minutes(n):
    return timedelta(minutes=n)


class LiveBase(PrepBase):
    def live(self, title="Live mock", starts_in=60, duration=30, late=10, category=None, **over):
        """A published live test starting `starts_in` minutes from now (negative = already started)."""
        start = timezone.now() + minutes(starts_in)
        return self.make_test(
            title, category or self.ssc, is_live=True, start_at=start, end_at=start + minutes(duration),
            late_join_minutes=late, allow_pause=False, **over,
        )

    def start(self, exam):
        return self.client.post(f"/api/student/exams/{exam.public_id}/start/")


class LiveAdminTests(LiveBase):
    def setUp(self):
        super().setUp()
        self.client.force_authenticate(self.admin)

    def post(self, **body):
        base = {"title": "Live 1", "category": self.ssc.id, "duration_minutes": 30, "is_live": True}
        base.update(body)
        return self.client.post("/api/exams/", base, format="json")

    def iso(self, minutes_from_now):
        return (timezone.now() + minutes(minutes_from_now)).isoformat()

    def test_a_live_test_needs_a_start_time(self):
        r = self.post()
        self.assertEqual(r.status_code, 400)
        self.assertIn("start_at", r.data)

    def test_the_end_is_worked_out_as_start_plus_duration(self):
        start = self.iso(120)
        r = self.post(start_at=start, end_at=self.iso(999))  # whatever end is typed is replaced
        self.assertEqual(r.status_code, 201, r.content)
        exam = Exam.objects.get(pk=r.data["id"])
        self.assertEqual(exam.end_at - exam.start_at, minutes(30))
        self.assertTrue(exam.is_live)
        self.assertEqual(exam.late_join_minutes, 10)

    def test_changing_the_duration_moves_the_end(self):
        eid = self.post(start_at=self.iso(120)).data["id"]
        r = self.client.patch(f"/api/exams/{eid}/", {"duration_minutes": 45}, format="json")
        self.assertEqual(r.status_code, 200, r.content)
        exam = Exam.objects.get(pk=eid)
        self.assertEqual(exam.end_at - exam.start_at, minutes(45))

    def test_pausing_is_switched_off_for_a_live_test(self):
        r = self.post(start_at=self.iso(120), allow_pause=True)
        self.assertFalse(r.data["allow_pause"])

    def test_a_normal_test_is_untouched(self):
        r = self.client.post("/api/exams/", {"title": "Normal", "category": self.ssc.id, "duration_minutes": 30}, format="json")
        self.assertFalse(r.data["is_live"])
        self.assertTrue(r.data["allow_pause"])
        self.assertIsNone(r.data["end_at"])

    def test_the_late_join_window_is_limited(self):
        self.assertEqual(self.post(start_at=self.iso(120), late_join_minutes=500).status_code, 400)
        r = self.post(start_at=self.iso(120), late_join_minutes=0)
        self.assertEqual(r.status_code, 201)

    def test_a_live_test_whose_start_has_passed_cannot_be_published(self):
        exam = self.make_test("Old live", self.ssc, status="draft", is_live=True, start_at=timezone.now() - minutes(5),
                              end_at=timezone.now() + minutes(25))
        r = self.client.post(f"/api/exams/{exam.id}/publish/")
        self.assertEqual(r.status_code, 400)
        self.assertTrue(any("future" in e for e in r.data["errors"]))

    def test_a_live_test_with_a_future_start_publishes(self):
        exam = self.make_test("New live", self.ssc, status="draft", is_live=True, start_at=timezone.now() + minutes(60),
                              end_at=timezone.now() + minutes(90))
        self.assertEqual(self.client.post(f"/api/exams/{exam.id}/publish/").status_code, 200)

    def test_live_settings_are_locked_once_published_but_the_start_can_move(self):
        exam = self.live(starts_in=60)
        r = self.client.patch(f"/api/exams/{exam.id}/", {"is_live": False}, format="json")
        self.assertEqual(r.status_code, 400)
        r = self.client.patch(f"/api/exams/{exam.id}/", {"late_join_minutes": 30}, format="json")
        self.assertEqual(r.status_code, 400)
        r = self.client.patch(f"/api/exams/{exam.id}/", {"start_at": self.iso(180)}, format="json")
        self.assertEqual(r.status_code, 200, r.content)
        exam.refresh_from_db()
        self.assertEqual(exam.end_at - exam.start_at, minutes(30))  # the end moved with it

    def test_the_admin_list_can_show_only_live_tests(self):
        self.live("A live one")
        self.make_test("Plain test", self.ssc)
        r = self.client.get("/api/exams/", {"is_live": "true"})
        self.assertEqual([e["title"] for e in r.data["results"]], ["A live one"])


class LiveStudentTests(LiveBase):
    def setUp(self):
        super().setUp()
        self.login()

    def card(self, exam):
        return next(c for c in self.client.get("/api/student/exams/").data if c["id"] == exam.public_id)

    def test_before_the_start_nobody_can_begin(self):
        exam = self.live(starts_in=30)
        c = self.card(exam)
        self.assertEqual((c["status"], c["can_start"]), ("upcoming", False))
        self.assertIn("live test", c["reason"])
        self.assertEqual(self.start(exam).status_code, 403)

    def test_it_opens_at_the_start_and_everyone_ends_at_the_same_moment(self):
        exam = self.live(starts_in=-2, duration=30)
        r = self.start(exam)
        self.assertEqual(r.status_code, 201, r.content)
        a = Attempt.objects.get(public_id=r.data["attempt_id"])
        self.assertAlmostEqual((a.deadline - exam.start_at).total_seconds(), 30 * 60, delta=2)
        self.assertAlmostEqual(self.client.get(f"/api/student/attempts/{a.public_id}/").data["remaining_seconds"], 28 * 60, delta=8)

    def test_a_late_joiner_gets_the_time_that_is_left_not_the_full_duration(self):
        exam = self.live(starts_in=-8, duration=30, late=10)
        aid = self.start(exam).data["attempt_id"]
        left = self.client.get(f"/api/student/attempts/{aid}/").data["remaining_seconds"]
        self.assertLess(left, 23 * 60)
        self.assertGreater(left, 21 * 60)

    def test_two_students_share_the_same_end_time(self):
        exam = self.live(starts_in=-1, duration=30, late=10)
        first = Attempt.objects.get(public_id=self.start(exam).data["attempt_id"])
        self.client.force_authenticate(self.other)
        second = Attempt.objects.get(public_id=self.start(exam).data["attempt_id"])
        self.assertEqual(first.deadline, second.deadline)

    def test_joining_closes_after_the_late_join_window(self):
        exam = self.live(starts_in=-15, duration=30, late=10)
        c = self.card(exam)
        self.assertEqual((c["status"], c["can_start"]), ("missed", False))
        self.assertIn("closed", c["reason"])
        self.assertEqual(self.start(exam).status_code, 403)

    def test_someone_already_inside_can_keep_going_after_joining_closes(self):
        exam = self.live(starts_in=-2, duration=30, late=10)
        aid = self.start(exam).data["attempt_id"]
        shift = minutes(20)
        Exam.objects.filter(pk=exam.pk).update(start_at=exam.start_at - shift, end_at=exam.end_at - shift + minutes(25))
        Attempt.objects.filter(public_id=aid).update(deadline=timezone.now() + minutes(5))
        self.assertEqual(self.card(Exam.objects.get(pk=exam.pk))["status"], "in_progress")
        self.assertEqual(self.client.get(f"/api/student/attempts/{aid}/").status_code, 200)

    def test_a_student_who_took_it_sees_completed_after_joining_closes(self):
        exam = self.live(starts_in=-1, duration=30, late=10)
        aid = self.start(exam).data["attempt_id"]
        self.client.post(f"/api/student/attempts/{aid}/submit/")
        shift = minutes(20)
        Exam.objects.filter(pk=exam.pk).update(start_at=exam.start_at - shift, end_at=exam.end_at - shift)
        self.assertEqual(self.card(Exam.objects.get(pk=exam.pk))["status"], "completed")

    def test_cards_say_it_is_live_and_when_joining_closes(self):
        exam = self.live(starts_in=30, late=15)
        c = self.card(exam)
        self.assertTrue(c["is_live"])
        closes = c["join_closes_at"]  # the test client keeps real datetimes
        self.assertAlmostEqual((closes - exam.start_at).total_seconds(), 15 * 60, delta=1)
        plain = self.make_test("Plain", self.ssc)
        self.assertFalse(self.card(plain)["is_live"])
        self.assertIsNone(self.card(plain)["join_closes_at"])

    def test_the_live_filter_lists_only_live_tests_that_have_not_finished_soonest_first(self):
        later = self.live("Later live", starts_in=180)
        soon = self.live("Soon live", starts_in=30)
        self.live("Finished live", starts_in=-90, duration=30)
        self.make_test("Plain test", self.ssc)
        r = self.client.get("/api/student/exams/", {"live": "1"})
        self.assertEqual([c["title"] for c in r.data], ["Soon live", "Later live"])
        self.assertEqual([soon.public_id, later.public_id], [c["id"] for c in r.data])

    def test_the_live_filter_can_be_limited_to_one_exam(self):
        self.live("SSC live", category=self.ssc)
        self.live("Bank live", category=self.bank)
        r = self.client.get("/api/student/exams/", {"live": "1", "category": self.bank.public_id})
        self.assertEqual([c["title"] for c in r.data], ["Bank live"])

    def test_the_detail_page_has_the_server_clock_for_the_countdown(self):
        exam = self.live(starts_in=30)
        d = self.client.get(f"/api/student/exams/{exam.public_id}/").data
        self.assertTrue(d["is_live"])
        self.assertIn("server_now", d)
        self.assertEqual(d["late_join_minutes"], 10)

    def test_a_normal_test_behaves_as_before(self):
        exam = self.make_test("Plain", self.ssc)
        self.assertEqual(self.start(exam).status_code, 201)


class LiveLinkIsPrivateTests(LiveBase):
    """The share link opens only for signed-in students who are allowed to take the test."""

    def setUp(self):
        super().setUp()
        self.login()

    def link(self, exam):
        return self.client.get(f"/api/student/live/{exam.public_id}/")

    def test_a_signed_in_student_sees_the_timing(self):
        exam = self.live("Sunday live", starts_in=60, duration=30, late=10)
        r = self.link(exam)
        self.assertEqual(r.status_code, 200, r.content)
        d = r.data
        self.assertEqual((d["title"], d["state"], d["duration_minutes"], d["late_join_minutes"]), ("Sunday live", "upcoming", 30, 10))
        self.assertEqual(d["category_name"], "SSC")
        self.assertTrue(d["is_live"])
        self.assertIn("server_now", d)
        self.assertEqual(r["Cache-Control"], "no-store")

    def test_there_is_no_public_version_of_the_link(self):
        exam = self.live("Sunday live")
        self.client.force_authenticate(None)
        self.assertEqual(self.link(exam).status_code, 401)
        self.assertEqual(self.client.get(f"/api/public/live/{exam.public_id}/").status_code, 404)

    def test_only_students_can_open_it(self):
        exam = self.live("Sunday live")
        for who in (self.admin,):
            self.client.force_authenticate(who)
            self.assertEqual(self.link(exam).status_code, 403)

    def test_it_never_reveals_the_questions(self):
        exam = self.live()
        text = str(self.link(exam).data)
        self.assertNotIn("Live mock question", text)
        self.assertNotIn("options", text)

    def test_the_state_follows_the_clock(self):
        cases = {-1: "live", -12: "joining_closed", -45: "ended", 30: "upcoming"}
        for starts_in, state in cases.items():
            exam = self.live(f"L{starts_in}", starts_in=starts_in, duration=30, late=10)
            self.assertEqual(self.link(exam).data["state"], state, starts_in)

    def test_it_says_where_the_student_stands(self):
        exam = self.live("Mine", starts_in=-1, duration=30, late=10)
        self.assertTrue(self.link(exam).data["can_start"])
        aid = self.start(exam).data["attempt_id"]
        d = self.link(exam).data
        self.assertEqual((d["status"], d["active_attempt_id"]), ("in_progress", aid))

    def test_the_family_is_named(self):
        from exams.models import ExamCategory, ExamGroup

        group = ExamGroup.objects.create(name="SSC Exams")
        cgl = ExamCategory.objects.create(name="SSC CGL", group=group)
        exam = self.live("CGL live", category=cgl)
        d = self.link(exam).data
        self.assertEqual((d["family"], d["category_name"]), ("SSC Exams", "SSC CGL"))

    def test_drafts_archived_and_normal_tests_have_no_live_page(self):
        draft = self.live("Draft live", status="draft")
        archived = self.live("Archived live", status="archived")
        plain = self.make_test("Plain", self.ssc)
        for e in (draft, archived, plain):
            self.assertEqual(self.link(e).status_code, 404, e.title)
        self.assertEqual(self.client.get("/api/student/live/nonexistent/").status_code, 404)

    def test_a_student_the_test_was_not_given_to_cannot_even_see_it(self):
        exam = self.live("Private live", starts_in=-1, access_type="assigned")
        self.assertEqual(self.link(exam).status_code, 404)  # same answer as for a test that does not exist
        self.assertEqual(self.start(exam).status_code, 404)

    def test_a_student_the_test_was_given_to_can_open_it(self):
        exam = self.live("Private live", starts_in=-1, access_type="assigned")
        exam.assigned_students.add(self.stu)
        self.assertEqual(self.link(exam).status_code, 200)
        self.assertEqual(self.start(exam).status_code, 201)

    def test_a_batch_test_opens_for_the_batch_only(self):
        from exams.models import Batch

        exam = self.live("Batch live", starts_in=-1, access_type="batch")
        batch = Batch.objects.create(name="Evening batch")
        batch.students.add(self.stu)
        exam.assigned_batches.add(batch)
        self.assertEqual(self.link(exam).status_code, 200)
        self.client.force_authenticate(self.other)
        self.assertEqual(self.link(exam).status_code, 404)
