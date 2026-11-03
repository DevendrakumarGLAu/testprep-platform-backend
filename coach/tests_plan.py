from datetime import timedelta
from io import StringIO

from django.core.management import call_command
from django.utils import timezone

from attempts.tests import EngineBase, user
from notifications.models import Notification

from .models import StudyPlan, StudyTask
from .reminders import send_plan_reminders


class PlanBase(EngineBase):
    def exam_in(self, days):
        return timezone.localdate() + timedelta(days=days)

    def make_plan(self, days=10, **extra):
        self.login()
        r = self.client.post("/api/student/study-plan/", {"exam_date": self.exam_in(days).isoformat(), "target_name": "SSC CGL", **extra}, format="json")
        self.assertEqual(r.status_code, 201, r.content)
        return r.data

    def finish(self, exam):
        attempt = self.start(exam)
        for q in self.questions(attempt):
            self.answer(attempt, q["id"], self.correct_option(q["id"]))
        self.assertEqual(self.client.post(f"/api/student/attempts/{attempt}/submit/").status_code, 200)


class CreatePlanTests(PlanBase):
    def test_no_plan_yet(self):
        self.login()
        self.assertIsNone(self.client.get("/api/student/study-plan/").data["plan"])

    def test_plan_has_a_test_revision_and_exam_day(self):
        data = self.make_plan(10)
        self.assertEqual(data["plan"]["days_left"], 10)
        self.assertEqual(data["plan"]["target_name"], "SSC CGL")
        kinds = {t["kind"] for t in data["today"]}
        self.assertIn("test", kinds)
        self.assertIn("revise", kinds)
        tasks = StudyTask.objects.filter(plan__student=self.stu)
        self.assertEqual(tasks.filter(kind="exam_day").count(), 1)
        self.assertEqual(tasks.filter(kind="exam_day").first().date, self.exam_in(10))
        self.assertFalse(tasks.filter(date=self.exam_in(10)).exclude(kind="exam_day").exists())  # nothing else on exam day

    def test_an_exam_can_be_chosen_by_its_public_code(self):
        from exams.models import ExamCategory

        cat = ExamCategory.objects.create(name="Railways")
        data = self.make_plan(10, category=cat.public_id)
        self.assertEqual(data["plan"]["category"], cat.public_id)
        self.assertEqual(StudyPlan.objects.get(student=self.stu).category_id, cat.id)

    def test_a_new_plan_replaces_the_old_one(self):
        self.make_plan(10)
        self.make_plan(5)
        self.assertEqual(StudyPlan.objects.filter(student=self.stu).count(), 1)
        self.assertEqual(StudyPlan.objects.get(student=self.stu).exam_date, self.exam_in(5))

    def test_bad_dates_and_minutes_are_refused(self):
        self.login()
        post = lambda body: self.client.post("/api/student/study-plan/", body, format="json")  # noqa: E731
        self.assertEqual(post({"exam_date": "nonsense"}).status_code, 400)
        self.assertEqual(post({"exam_date": timezone.localdate().isoformat()}).status_code, 400)
        self.assertEqual(post({"exam_date": self.exam_in(400).isoformat()}).status_code, 400)
        self.assertEqual(post({"exam_date": self.exam_in(10).isoformat(), "daily_minutes": 5}).status_code, 400)
        self.assertEqual(post({"exam_date": self.exam_in(10).isoformat(), "category": 99999}).status_code, 404)

    def test_only_students_and_only_your_own_plan(self):
        self.client.force_authenticate(self.admin)
        self.assertEqual(self.client.get("/api/student/study-plan/").status_code, 403)
        self.make_plan(8)
        task = StudyTask.objects.filter(plan__student=self.stu).first()
        self.login(self.other)
        self.assertIsNone(self.client.get("/api/student/study-plan/").data["plan"])
        self.assertEqual(self.client.post(f"/api/student/study-plan/tasks/{task.id}/done/", {"done": True}, format="json").status_code, 404)

    def test_delete_plan(self):
        self.make_plan(8)
        self.assertEqual(self.client.delete("/api/student/study-plan/").status_code, 204)
        self.assertIsNone(self.client.get("/api/student/study-plan/").data["plan"])

    def test_tests_that_cannot_be_started_are_never_scheduled(self):
        closed = self.make_exam(title="Closed window", end_at=timezone.now() - timedelta(days=1), start_at=timezone.now() - timedelta(days=5))
        not_yet = self.make_exam(title="Opens later", start_at=timezone.now() + timedelta(days=3), end_at=timezone.now() + timedelta(days=6))
        self.make_plan(10)
        self.assertFalse(StudyTask.objects.filter(exam__in=[closed, not_yet]).exists())

    def test_drafts_are_never_scheduled(self):
        draft = self.make_exam(publish=False, title="Hidden draft")
        self.make_plan(10)
        self.assertFalse(StudyTask.objects.filter(exam=draft).exists())


class ProgressTests(PlanBase):
    def test_taking_the_scheduled_test_ticks_it_off(self):
        data = self.make_plan(10)
        test = next(t for t in data["today"] if t["kind"] == "test")
        exam = StudyTask.objects.get(pk=test["id"]).exam
        self.finish(exam)
        after = self.client.get("/api/student/study-plan/").data
        self.assertTrue(next(t for t in after["today"] if t["id"] == test["id"])["done"])
        self.assertGreaterEqual(after["plan"]["done"], 1)

    def test_tick_and_untick_a_task(self):
        data = self.make_plan(8)
        rev = next(t for t in data["today"] if t["kind"] == "revise")
        url = f"/api/student/study-plan/tasks/{rev['id']}/done/"
        done = self.client.post(url, {"done": True}, format="json").data
        self.assertTrue(next(t for t in done["today"] if t["id"] == rev["id"])["done"])
        undone = self.client.post(url, {"done": False}, format="json").data
        self.assertFalse(next(t for t in undone["today"] if t["id"] == rev["id"])["done"])

    def test_missed_tasks_show_as_overdue_and_reschedule_moves_the_tests(self):
        self.make_plan(10)
        plan = StudyPlan.objects.get(student=self.stu)
        plan.tasks.filter(date=timezone.localdate()).update(date=timezone.localdate() - timedelta(days=1))
        data = self.client.get("/api/student/study-plan/").data
        self.assertGreaterEqual(data["plan"]["overdue_count"], 2)
        result = self.client.post("/api/student/study-plan/reschedule/").data
        self.assertGreaterEqual(result["moved"], 1)
        self.assertEqual(result["plan"]["overdue_count"], 0)
        self.assertFalse(plan.tasks.filter(done=False, date__lt=timezone.localdate()).exists())

    def test_reschedule_without_a_plan(self):
        self.login()
        self.assertEqual(self.client.post("/api/student/study-plan/reschedule/").status_code, 404)


class ReminderTests(PlanBase):
    def test_reminder_goes_out_once_a_day_to_students_with_work_left(self):
        self.make_plan(10)
        user("planless")  # no plan: nothing for them
        self.assertEqual(send_plan_reminders(), 1)
        self.assertEqual(send_plan_reminders(), 0)  # same day, same message
        note = Notification.objects.get(user=self.stu)
        self.assertEqual(note.link, "/student/plan")

    def test_nothing_is_sent_when_today_is_done(self):
        self.make_plan(10)
        StudyTask.objects.filter(plan__student=self.stu).update(done=True)
        self.assertEqual(send_plan_reminders(), 0)

    def test_management_command(self):
        self.make_plan(10)
        out = StringIO()
        call_command("send_study_reminders", stdout=out)
        self.assertIn("Reminders sent: 1", out.getvalue())


class SubjectPlanTests(PlanBase):
    """A plan for an exam with subjects is organised by subject."""

    def setUp(self):
        super().setUp()
        from exams.models import ExamCategory
        from questionbank.models import Subject

        self.maths = Subject.objects.create(name="Maths")
        self.gk = Subject.objects.create(name="GK")
        self.category = ExamCategory.objects.create(name="SSC")
        self.category.subjects.set([self.maths, self.gk])
        self.exam.category = self.category
        self.exam.save()
        # one subject test for Maths, none for GK; questions to practise in both
        self.maths_test = self.make_exam(title="Maths drill", test_type="sectional", subject=self.maths, category=self.category)
        for subject in (self.maths, self.gk):
            for i in range(4):
                q = self.make_question(f"{subject.name} practice {i}")
                q.subject = subject
                q.save()

    def tasks(self):
        return list(StudyTask.objects.filter(plan__student=self.stu).select_related("subject", "exam").order_by("date", "id"))

    def test_each_study_day_has_a_focus_subject_and_both_subjects_are_covered(self):
        self.make_plan(12, category=self.category.id)
        subjects = {t.subject.name for t in self.tasks() if t.subject}
        self.assertEqual(subjects, {"Maths", "GK"})
        for day in {t.date for t in self.tasks() if t.kind != "exam_day"} - {self.exam_in(11)}:
            tagged = {t.subject_id for t in self.tasks() if t.date == day and t.subject_id}
            self.assertLessEqual(len(tagged), 1, "one focus subject a day")

    def test_a_subject_with_a_subject_test_gets_it_and_one_without_gets_practice(self):
        self.make_plan(12, category=self.category.id)
        tests = [t for t in self.tasks() if t.kind == "test" and t.subject]
        self.assertTrue(tests)
        self.assertTrue(all(t.subject.name == "Maths" and t.exam_id == self.maths_test.id for t in tests))
        self.assertTrue(all(t.title.startswith("Maths: ") for t in tests))
        gk_work = [t for t in self.tasks() if t.subject and t.subject.name == "GK"]
        self.assertTrue(gk_work)
        self.assertTrue(all(t.kind == "practice" and t.title == "Practise GK" for t in gk_work))

    def test_a_full_test_still_comes_round_every_few_days(self):
        self.make_plan(14, category=self.category.id)
        full = [t for t in self.tasks() if t.kind == "test" and not t.subject]
        self.assertTrue(full)
        self.assertTrue(all(t.title.startswith("Full test: ") for t in full))
        self.assertTrue(all(t.exam_id != self.maths_test.id for t in full))

    def test_a_weak_subject_comes_round_more_often_than_a_strong_one(self):
        from attempts.models import Attempt, AttemptQuestion

        self.login()
        attempt = Attempt.objects.create(
            student=self.stu, exam=self.exam, status=Attempt.Status.SUBMITTED, submitted_at=timezone.now(), deadline=timezone.now(), attempt_number=1,
            exam_version=1, duration_minutes=30, total_marks=10, passing_marks=2, negative_marking_enabled=True,
        )
        from questionbank.models import Question

        # 5 answers in Maths, all right; 5 in GK, all wrong
        order = 0
        for subject, right in ((self.maths, True), (self.gk, False)):
            for _ in range(5):
                q = Question.objects.filter(subject=subject).first()
                order += 1
                AttemptQuestion.objects.create(
                    attempt=attempt, question=q, order=order, section_name="S", text=q.text, options=[], is_correct=right, marks=1, negative_marks=0,
                )
        self.make_plan(30, category=self.category.id)
        days = {"Maths": set(), "GK": set()}
        for t in self.tasks():
            if t.subject:
                days[t.subject.name].add(t.date)
        self.assertGreater(len(days["GK"]), len(days["Maths"]))

    def test_without_subjects_the_plan_is_the_old_test_a_day_plan(self):
        from exams.models import ExamCategory

        bare = ExamCategory.objects.create(name="Other")
        self.make_plan(10, category=bare.id)
        self.assertFalse([t for t in self.tasks() if t.subject_id])

    def test_the_plan_reports_progress_per_subject_weakest_first(self):
        data = self.make_plan(12, category=self.category.id)
        names = [s["name"] for s in data["subjects"]]
        self.assertEqual(set(names), {"Maths", "GK"})
        maths = next(s for s in data["subjects"] if s["name"] == "Maths")
        self.assertGreaterEqual(maths["tests"], 1)
        self.assertEqual(maths["done"], 0)
        self.assertIsNone(maths["accuracy"])
        row = next(t for t in data["upcoming"][0]["tasks"] if t["subject"]) if data["upcoming"] else next(t for t in data["today"] if t["subject"])
        self.assertIn("subject_id", row)

    def test_a_subject_practice_task_links_to_that_subject(self):
        data = self.make_plan(12, category=self.category.id)
        task = next(t for t in StudyTask.objects.filter(plan__student=self.stu, kind="practice", subject=self.gk))
        from coach.plan import link_for

        self.assertEqual(link_for(task), f"/student/practice?subject={self.gk.id}&name=GK")

    def test_refresh_plans_the_coming_days_again_and_keeps_ticked_work(self):
        self.make_plan(12, category=self.category.id)
        plan = StudyPlan.objects.get(student=self.stu)
        future = plan.tasks.filter(date__gt=timezone.localdate()).exclude(kind="exam_day")
        kept = future.first()
        kept.done = True
        kept.save()
        before_today = list(plan.tasks.filter(date=timezone.localdate()).values_list("id", flat=True))
        r = self.client.post("/api/student/study-plan/refresh/")
        self.assertEqual(r.status_code, 200, r.content)
        self.assertGreater(r.data["replanned"], 0)
        self.assertTrue(plan.tasks.filter(pk=kept.pk, done=True).exists())  # ticked work stays
        self.assertEqual(list(plan.tasks.filter(date=timezone.localdate()).values_list("id", flat=True)), before_today)  # today stays
        self.assertEqual(plan.tasks.filter(kind="exam_day").count(), 1)

    def test_refresh_needs_a_plan_and_a_student(self):
        self.login()
        self.assertEqual(self.client.post("/api/student/study-plan/refresh/").status_code, 404)
        self.client.force_authenticate(self.admin)
        self.assertEqual(self.client.post("/api/student/study-plan/refresh/").status_code, 403)
