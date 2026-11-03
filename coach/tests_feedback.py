from attempts.tests import EngineBase
from notifications.models import Notification

from .models import Feedback


class FeedbackTests(EngineBase):
    def send(self, body=None, user=None):
        self.login(user)
        return self.client.post("/api/feedback/", body or {"kind": "bug", "message": "The timer froze on my phone", "page": "/student/attempt/x"}, format="json")

    def test_a_student_can_send_a_note_and_staff_are_told(self):
        r = self.send()
        self.assertEqual(r.status_code, 201, r.content)
        item = Feedback.objects.get()
        self.assertEqual((item.user, item.kind, item.status), (self.stu, "bug", "new"))
        self.assertEqual(item.page, "/student/attempt/x")
        self.assertTrue(Notification.objects.filter(user=self.admin, link="/admin/feedback").exists())

    def test_an_unknown_kind_becomes_other_and_the_browser_is_kept(self):
        self.login()
        self.client.post("/api/feedback/", {"kind": "nonsense", "message": "Just saying hello"}, format="json", HTTP_USER_AGENT="TestBrowser/1.0")
        item = Feedback.objects.get()
        self.assertEqual((item.kind, item.device), ("other", "TestBrowser/1.0"))

    def test_too_short_too_long_and_signed_out_are_refused(self):
        self.assertEqual(self.send({"message": "hi"}).status_code, 400)
        self.assertEqual(self.send({"message": "x" * 2001}).status_code, 400)
        self.client.force_authenticate(None)
        self.assertEqual(self.client.post("/api/feedback/", {"message": "Not signed in at all"}, format="json").status_code, 401)
        self.assertEqual(Feedback.objects.count(), 0)

    def test_one_person_cannot_flood_the_inbox(self):
        for i in range(20):
            self.assertEqual(self.send({"message": f"Note number {i}"}).status_code, 201)
        r = self.send({"message": "One too many notes"})
        self.assertEqual(r.status_code, 400)
        self.assertEqual(Feedback.objects.count(), 20)

    def test_staff_read_the_inbox_and_tick_notes_off(self):
        self.send()
        self.send({"kind": "idea", "message": "Please add a dark mode toggle"})
        self.client.force_authenticate(self.admin)
        data = self.client.get("/api/feedback/").data
        self.assertEqual(data["new_count"], 2)
        self.assertEqual(data["results"][0]["kind"], "idea")  # newest first
        self.assertEqual(data["results"][0]["from_name"], "stu")
        note = data["results"][1]["id"]
        done = self.client.post(f"/api/feedback/{note}/status/", {"status": "done"}, format="json")
        self.assertEqual(done.data["status"], "done")
        self.assertEqual(self.client.get("/api/feedback/?status=new").data["new_count"], 1)
        self.assertEqual(len(self.client.get("/api/feedback/?status=new").data["results"]), 1)
        self.assertEqual(self.client.post(f"/api/feedback/{note}/status/", {"status": "later"}, format="json").status_code, 400)

    def test_students_cannot_read_the_inbox_or_change_a_note(self):
        self.send()
        note = Feedback.objects.get().id
        self.login()
        self.assertEqual(self.client.get("/api/feedback/").status_code, 403)
        self.assertEqual(self.client.post(f"/api/feedback/{note}/status/", {"status": "done"}, format="json").status_code, 403)

    def test_a_note_survives_the_sender_leaving(self):
        self.send()
        self.stu.delete()
        self.client.force_authenticate(self.admin)
        row = self.client.get("/api/feedback/").data["results"][0]
        self.assertEqual(row["from_name"], "Someone who has left")
