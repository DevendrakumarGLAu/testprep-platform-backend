from datetime import timedelta
from io import StringIO

from django.core.management import call_command
from django.utils import timezone

from accounts.models import Role, User
from attempts.tests import EngineBase
from exams.models import Batch, Exam, ExamCategory, Preparation

from . import services
from .models import Notification

PW = "Str0ng!pass99"


class NotifBase(EngineBase):
    def setUp(self):
        super().setUp()
        self.third = User.objects.create_user(username="third", email="third@x.com", password=PW, role=Role.STUDENT)

    def inbox(self, who):
        self.client.force_authenticate(who)
        return self.client.get("/api/notifications/").data


class InboxTests(NotifBase):
    def test_a_person_sees_only_their_own_messages_newest_first(self):
        services.notify([self.stu], "announcement", "First")
        services.notify([self.stu], "announcement", "Second")
        services.notify([self.other], "announcement", "Not yours")
        data = self.inbox(self.stu)
        self.assertEqual([n["title"] for n in data["results"]], ["Second", "First"])
        self.assertEqual(data["unread"], 2)

    def test_unread_count_and_marking_read(self):
        services.notify([self.stu], "announcement", "A")
        services.notify([self.stu], "announcement", "B")
        self.client.force_authenticate(self.stu)
        self.assertEqual(self.client.get("/api/notifications/unread-count/").data["unread"], 2)
        first = self.client.get("/api/notifications/").data["results"][0]["id"]
        self.assertEqual(self.client.post("/api/notifications/read/", {"ids": [first]}, format="json").data["marked"], 1)
        self.assertEqual(self.client.get("/api/notifications/unread-count/").data["unread"], 1)
        self.assertEqual(self.client.post("/api/notifications/read/", {"all": True}, format="json").data["marked"], 1)
        self.assertEqual(self.client.get("/api/notifications/unread-count/").data["unread"], 0)
        self.assertEqual(len(self.client.get("/api/notifications/?unread=1").data["results"]), 0)

    def test_cannot_mark_someone_elses_message_as_read(self):
        services.notify([self.other], "announcement", "Theirs")
        theirs = Notification.objects.get().id
        self.client.force_authenticate(self.stu)
        self.assertEqual(self.client.post("/api/notifications/read/", {"ids": [theirs]}, format="json").data["marked"], 0)
        self.assertIsNone(Notification.objects.get().read_at)

    def test_bad_requests(self):
        self.client.force_authenticate(self.stu)
        self.assertEqual(self.client.post("/api/notifications/read/", {}, format="json").status_code, 400)
        self.assertEqual(self.client.post("/api/notifications/read/", {"ids": ["x"]}, format="json").status_code, 400)

    def test_must_be_signed_in(self):
        self.client.force_authenticate(None)
        self.assertEqual(self.client.get("/api/notifications/").status_code, 401)

    def test_the_same_reference_is_sent_once_per_person(self):
        self.assertEqual(services.notify([self.stu, self.other], "reminder", "Soon", ref="start:1"), 2)
        self.assertEqual(services.notify([self.stu, self.other, self.third], "reminder", "Soon", ref="start:1"), 1)
        self.assertEqual(Notification.objects.count(), 3)


class AnnouncementTests(NotifBase):
    def announce(self, **body):
        self.client.force_authenticate(self.admin)
        return self.client.post("/api/announcements/", {"title": "Holiday", "body": "No tests on Friday", **body}, format="json")

    def test_sent_to_every_active_student_but_not_admins(self):
        User.objects.filter(pk=self.third.pk).update(is_active=False)
        r = self.announce()
        self.assertEqual((r.status_code, r.data["sent"]), (201, 2))
        self.assertEqual(Notification.objects.filter(user=self.admin).count(), 0)
        self.assertEqual(self.inbox(self.stu)["results"][0]["title"], "Holiday")

    def test_to_the_students_preparing_for_one_exam(self):
        ssc = ExamCategory.objects.create(name="SSC")
        Preparation.objects.create(student=self.stu, category=ssc)
        r = self.announce(audience="exam", target=ssc.id)
        self.assertEqual(r.data["sent"], 1)
        self.assertEqual(Notification.objects.get().user, self.stu)

    def test_to_one_batch(self):
        batch = Batch.objects.create(name="Morning")
        batch.students.add(self.other)
        self.assertEqual(self.announce(audience="batch", target=batch.id).data["sent"], 1)
        self.assertEqual(Notification.objects.get().user, self.other)

    def test_checks_the_request(self):
        self.assertEqual(self.announce(title="").status_code, 400)
        self.assertEqual(self.announce(body="x" * 1001).status_code, 400)
        self.assertEqual(self.announce(audience="everyone").status_code, 400)
        self.assertEqual(self.announce(audience="exam", target=99999).status_code, 404)
        self.assertEqual(self.announce(link="https://evil.example").status_code, 400)
        self.assertEqual(Notification.objects.count(), 0)

    def test_students_cannot_announce(self):
        self.client.force_authenticate(self.stu)
        self.assertEqual(self.client.post("/api/announcements/", {"title": "x"}, format="json").status_code, 403)


class EventTests(NotifBase):
    def test_publishing_a_test_tells_the_students_who_can_take_it(self):
        ssc = ExamCategory.objects.create(name="SSC")
        Preparation.objects.create(student=self.stu, category=ssc)
        draft = self.make_exam(publish=False, category=ssc)
        self.client.force_authenticate(self.admin)
        self.assertEqual(self.client.post(f"/api/exams/{draft.id}/publish/").status_code, 200)
        n = Notification.objects.get()
        self.assertEqual((n.user, n.kind), (self.stu, "new_test"))
        self.assertIn(draft.public_id, n.link)

    def test_assigned_tests_notify_only_the_assigned_students(self):
        draft = self.make_exam(publish=False, access_type=Exam.Access.ASSIGNED)
        draft.assigned_students.add(self.other)
        self.client.force_authenticate(self.admin)
        self.client.post(f"/api/exams/{draft.id}/publish/")
        self.assertEqual(list(Notification.objects.values_list("user", flat=True)), [self.other.id])

    def test_releasing_results_tells_those_who_took_the_test(self):
        exam = self.make_exam(result_release=Exam.ResultRelease.MANUAL)
        self.client.force_authenticate(self.stu)
        a = self.start(exam)
        self.client.post(f"/api/student/attempts/{a}/submit/")
        self.client.force_authenticate(self.admin)
        self.assertEqual(self.client.post(f"/api/exams/{exam.id}/release-results/").status_code, 200)
        self.assertEqual(list(Notification.objects.filter(kind="result").values_list("user", flat=True)), [self.stu.id])
        self.client.post(f"/api/exams/{exam.id}/hide-results/")
        self.client.post(f"/api/exams/{exam.id}/release-results/")  # releasing again does not send it twice
        self.assertEqual(Notification.objects.filter(kind="result").count(), 1)

    def test_a_reply_to_a_question_report_reaches_the_student(self):
        self.client.force_authenticate(self.stu)
        a = self.start()
        for q in self.questions(a):
            self.answer(a, q["id"], self.wrong_option(q["id"]))
        self.client.post(f"/api/student/attempts/{a}/submit/")
        self.client.post(f"/api/student/attempts/{a}/questions/1/report/", {"reason": "typo"}, format="json")
        from questionbank.models import QuestionReport
        rid = QuestionReport.objects.get().id
        self.client.force_authenticate(self.admin)
        self.client.patch(f"/api/question-reports/{rid}/", {"status": "resolved", "admin_note": "Fixed it"}, format="json")
        n = Notification.objects.get(kind="report")
        self.assertEqual((n.user, n.title, n.body), (self.stu, "Your report was fixed", "Fixed it"))


class ReminderTests(NotifBase):
    def scheduled(self, minutes, **over):
        start = timezone.now() + timedelta(minutes=minutes)
        return self.make_exam(start_at=start, end_at=start + timedelta(hours=3), access_type=Exam.Access.ASSIGNED, **over)

    def test_students_are_reminded_of_a_test_starting_within_the_hour_once(self):
        exam = self.scheduled(30)
        exam.assigned_students.add(self.stu, self.other)
        self.assertEqual(services.send_start_reminders(), 2)
        self.assertEqual(services.send_start_reminders(), 0)  # running the job again sends nothing new
        n = Notification.objects.filter(user=self.stu).get()
        self.assertEqual((n.kind, n.title), ("reminder", f"Starting soon: {exam.title}"))

    def test_tests_that_are_far_off_already_started_or_unpublished_are_ignored(self):
        far = self.scheduled(180)
        far.assigned_students.add(self.stu)
        started = self.scheduled(-10)
        started.assigned_students.add(self.stu)
        draft = self.scheduled(20, publish=False)
        draft.assigned_students.add(self.stu)
        self.assertEqual(services.send_start_reminders(), 0)

    def test_the_command_runs_and_reports(self):
        exam = self.scheduled(10)
        exam.assigned_students.add(self.stu)
        out = StringIO()
        call_command("send_reminders", stdout=out)
        self.assertIn("1 reminder(s) sent", out.getvalue())
