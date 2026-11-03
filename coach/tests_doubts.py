from unittest import mock

from attempts.models import Attempt
from attempts.tests import EngineBase, user
from accounts.models import Role
from notifications.models import Notification

from . import ai
from .models import Doubt


class DoubtBase(EngineBase):
    def setUp(self):
        super().setUp()
        self.login()
        self.attempt = self.start()
        for q in self.questions(self.attempt):
            self.answer(self.attempt, q["id"], self.wrong_option(q["id"]))
        self.client.post(f"/api/student/attempts/{self.attempt}/submit/")

    def ask(self, number=1, message="Why is this the right answer?"):
        return self.client.post(f"/api/student/attempts/{self.attempt}/questions/{number}/doubt/", {"message": message}, format="json")


class StudentDoubtTests(DoubtBase):
    def test_ask_a_doubt_from_the_review(self):
        r = self.ask()
        self.assertEqual(r.status_code, 201, r.content)
        self.assertEqual(r.data["status"], "open")
        self.assertEqual(len(r.data["replies"]), 1)
        self.assertEqual(r.data["selected_option_text"], "wrong")
        self.assertTrue(any(o["is_correct"] for o in r.data["options"]))
        self.assertEqual(Notification.objects.filter(user=self.admin, kind="doubt").count(), 1)  # teachers are told

    def test_asking_again_about_the_same_question_adds_to_the_thread(self):
        first = self.ask().data["id"]
        again = self.ask(message="I still do not understand.")
        self.assertEqual(again.status_code, 200)
        self.assertEqual(again.data["id"], first)
        self.assertEqual(len(again.data["replies"]), 2)
        self.assertEqual(Doubt.objects.count(), 1)

    def test_cannot_ask_before_the_test_is_submitted(self):
        fresh = self.make_exam(title="Fresh")
        attempt = self.start(fresh)
        r = self.client.post(f"/api/student/attempts/{attempt}/questions/1/doubt/", {"message": "Why is this so?"}, format="json")
        self.assertEqual(r.status_code, 400)

    def test_message_rules(self):
        self.assertEqual(self.ask(message="hi").status_code, 400)
        self.assertEqual(self.ask(message="x" * 2000).status_code, 400)

    def test_limit_on_waiting_doubts(self):
        for n in range(1, 4):
            self.assertEqual(self.ask(number=n).status_code, 201)
        with mock.patch("coach.doubt_views.OPEN_LIMIT", 3):
            fresh = self.make_exam(n=2, title="Another")
            attempt = self.start(fresh)
            for q in self.questions(attempt):
                self.answer(attempt, q["id"], self.wrong_option(q["id"]))
            self.client.post(f"/api/student/attempts/{attempt}/submit/")
            r = self.client.post(f"/api/student/attempts/{attempt}/questions/1/doubt/", {"message": "One more doubt please"}, format="json")
        self.assertEqual(r.status_code, 400)

    def test_list_detail_reply_and_resolve(self):
        doubt = self.ask().data["id"]
        self.assertEqual(len(self.client.get("/api/student/doubts/").data), 1)
        self.assertEqual(len(self.client.get("/api/student/doubts/?status=resolved").data), 0)
        self.assertEqual(self.client.get(f"/api/student/doubts/{doubt}/").status_code, 200)
        r = self.client.post(f"/api/student/doubts/{doubt}/reply/", {"body": "Thank you, one more thing."}, format="json")
        self.assertEqual(len(r.data["replies"]), 2)
        self.assertEqual(self.client.post(f"/api/student/doubts/{doubt}/resolve/").data["status"], "resolved")

    def test_students_cannot_see_each_others_doubts(self):
        doubt = self.ask().data["id"]
        self.login(self.other)
        self.assertEqual(self.client.get("/api/student/doubts/").data, [])
        self.assertEqual(self.client.get(f"/api/student/doubts/{doubt}/").status_code, 404)
        self.assertEqual(self.client.post(f"/api/student/doubts/{doubt}/reply/", {"body": "Not mine to answer"}, format="json").status_code, 404)


class TeacherDoubtTests(DoubtBase):
    def setUp(self):
        super().setUp()
        self.doubt = self.ask().data["id"]
        self.client.force_authenticate(self.admin)

    def test_inbox_counts_and_filters(self):
        data = self.client.get("/api/admin/doubts/").data
        self.assertEqual(data["counts"], {"open": 1, "answered": 0, "resolved": 0})
        self.assertEqual(len(data["results"]), 1)
        self.assertEqual(len(self.client.get("/api/admin/doubts/?status=answered").data["results"]), 0)
        self.assertEqual(len(self.client.get("/api/admin/doubts/?search=Question").data["results"]), 1)
        self.assertEqual(len(self.client.get("/api/admin/doubts/?search=zzzz").data["results"]), 0)

    def test_students_cannot_use_the_teacher_screens(self):
        self.login()
        self.assertEqual(self.client.get("/api/admin/doubts/").status_code, 403)
        self.assertEqual(self.client.post(f"/api/admin/doubts/{self.doubt}/reply/", {"body": "I am a student"}, format="json").status_code, 403)

    def test_reply_marks_answered_and_notifies_the_student(self):
        r = self.client.post(f"/api/admin/doubts/{self.doubt}/reply/", {"body": "Because the option says so."}, format="json")
        self.assertEqual(r.status_code, 200, r.content)
        self.assertEqual(r.data["status"], "answered")
        self.assertEqual(r.data["replies"][-1]["role"], "staff")
        note = Notification.objects.get(user=self.stu, kind="doubt")
        self.assertEqual(note.link, f"/student/doubts/{self.doubt}")

    def test_student_follow_up_reopens(self):
        self.client.post(f"/api/admin/doubts/{self.doubt}/reply/", {"body": "Here is the answer."}, format="json")
        self.login()
        self.assertEqual(self.client.post(f"/api/student/doubts/{self.doubt}/reply/", {"body": "But why that one?"}, format="json").data["status"], "open")

    def test_status_change(self):
        r = self.client.post(f"/api/admin/doubts/{self.doubt}/status/", {"status": "resolved"}, format="json")
        self.assertEqual(r.data["status"], "resolved")
        self.assertEqual(self.client.post(f"/api/admin/doubts/{self.doubt}/status/", {"status": "bogus"}, format="json").status_code, 400)

    def test_ai_helper_not_set_up(self):
        with mock.patch.dict("os.environ", {}, clear=False):
            import os
            os.environ.pop("ANTHROPIC_API_KEY", None)
            r = self.client.post(f"/api/admin/doubts/{self.doubt}/ai-suggest/")
        self.assertEqual(r.status_code, 503)
        self.assertIn("not set up", r.data["detail"])

    def test_ai_helper_drafts_but_does_not_send(self):
        with mock.patch.object(ai, "complete", return_value="Draft: the correct option is right because of X.") as fake:
            r = self.client.post(f"/api/admin/doubts/{self.doubt}/ai-suggest/")
        self.assertEqual(r.status_code, 200)
        self.assertIn("Draft", r.data["suggestion"])
        self.assertIn("Why is this the right answer?", fake.call_args[0][0] + "Why is this the right answer?")  # the prompt was built
        self.assertEqual(Doubt.objects.get(pk=self.doubt).replies.count(), 1)  # nothing was sent to the student

    def test_a_reply_sent_with_ai_help_is_marked(self):
        r = self.client.post(f"/api/admin/doubts/{self.doubt}/reply/", {"body": "A reply that the helper drafted.", "via_ai": True}, format="json")
        self.assertTrue(r.data["replies"][-1]["via_ai"])
