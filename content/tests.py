from rest_framework.test import APITestCase

from accounts.models import Role, User
from attempts.tests import EngineBase
from exams.models import Exam, ExamCategory
from questionbank.models import Question, QuestionOption, Subject

from .models import StudyNote, TestSeries

PW = "Str0ng!pass99"


class NoteTests(APITestCase):
    def setUp(self):
        self.admin = User.objects.create_user(username="adm", email="a@x.com", password=PW, role=Role.ADMIN)
        self.stu = User.objects.create_user(username="stu", email="s@x.com", password=PW, role=Role.STUDENT)
        self.cat = ExamCategory.objects.create(name="SSC")

    def make(self, **over):
        body = {"title": "October 2026 capsule", "body": "Point one\nPoint two", "is_published": True, **over}
        self.client.force_authenticate(self.admin)
        r = self.client.post("/api/study-notes/", body, format="json")
        self.assertEqual(r.status_code, 201, r.content)
        return r.data

    def test_admin_writes_a_note_in_english_hindi_or_both(self):
        self.make()
        self.make(title="", title_hi="अक्टूबर", body="", body_hi="बिंदु एक")
        self.assertEqual(StudyNote.objects.count(), 2)

    def test_a_note_needs_a_title_and_a_body_in_some_language(self):
        self.client.force_authenticate(self.admin)
        self.assertEqual(self.client.post("/api/study-notes/", {"title": "", "body": "x"}, format="json").status_code, 400)
        self.assertEqual(self.client.post("/api/study-notes/", {"title": "x", "body": ""}, format="json").status_code, 400)

    def test_students_see_only_published_notes_and_cannot_edit(self):
        pub = self.make(category=self.cat.id)
        self.make(title="Draft", is_published=False)
        self.client.force_authenticate(self.stu)
        r = self.client.get("/api/student/notes/")
        self.assertEqual([n["title"] for n in r.data["results"]], ["October 2026 capsule"])
        self.assertNotIn("body", r.data["results"][0])  # the list carries a summary only
        self.assertEqual(self.client.get(f"/api/student/notes/{pub['public_id']}/").data["body"], "Point one\nPoint two")
        draft = StudyNote.objects.get(title="Draft")
        self.assertEqual(self.client.get(f"/api/student/notes/{draft.public_id}/").status_code, 404)
        self.assertEqual(self.client.post("/api/study-notes/", {"title": "x", "body": "y"}, format="json").status_code, 403)

    def test_students_can_filter_and_search(self):
        self.make(category=self.cat.id)
        self.make(title="Other exam", body="nothing", category=None)
        self.client.force_authenticate(self.stu)
        self.assertEqual(len(self.client.get(f"/api/student/notes/?category={self.cat.public_id}").data["results"]), 1)
        self.assertEqual(len(self.client.get("/api/student/notes/?search=capsule").data["results"]), 1)

    def test_summary_is_shortened(self):
        self.make(body="x" * 500)
        self.client.force_authenticate(self.stu)
        self.assertLessEqual(len(self.client.get("/api/student/notes/").data["results"][0]["summary"]), 201)


class SeriesTests(EngineBase):
    def setUp(self):
        super().setUp()
        self.e2 = self.make_exam(n=2, max_attempts=0)
        self.draft = self.make_exam(n=2, publish=False)

    def make(self, ids, **over):
        self.client.force_authenticate(self.admin)
        r = self.client.post("/api/test-series/", {"title": "SSC CGL mocks", "exam_ids": ids, "is_published": True, **over}, format="json")
        self.assertEqual(r.status_code, 201, r.content)
        return r.data

    def test_admin_builds_an_ordered_series(self):
        s = self.make([self.e2.id, self.exam.id])
        self.assertEqual([e["id"] for e in s["exams"]], [self.e2.id, self.exam.id])
        r = self.client.patch(f"/api/test-series/{s['id']}/", {"exam_ids": [self.exam.id, self.e2.id]}, format="json")
        self.assertEqual([e["id"] for e in r.data["exams"]], [self.exam.id, self.e2.id])

    def test_bad_exam_lists_are_refused(self):
        self.client.force_authenticate(self.admin)
        for ids in ([self.exam.id, self.exam.id], [99999]):
            self.assertEqual(self.client.post("/api/test-series/", {"title": "x", "exam_ids": ids}, format="json").status_code, 400)

    def test_student_sees_progress_through_the_series(self):
        s = self.make([self.exam.id, self.e2.id])
        self.login()
        a = self.start(self.exam)
        for q in self.questions(a):
            self.answer(a, q["id"], self.correct_option(q["id"]))
        self.client.post(f"/api/student/attempts/{a}/submit/")
        row = self.client.get("/api/student/series/").data["results"][0]
        self.assertEqual((row["title"], row["total"], row["attempted"]), ("SSC CGL mocks", 2, 1))
        detail = self.client.get(f"/api/student/series/{row['id']}/")
        self.assertEqual([t["title"] for t in detail.data["tests"]], ["Mock", "Mock"])
        self.assertEqual((detail.data["total"], detail.data["attempted"]), (2, 1))
        self.assertEqual(s["title"], detail.data["title"])

    def test_unpublished_series_and_tests_the_student_cannot_see_are_hidden(self):
        self.make([self.exam.id], is_published=False, title="Hidden")
        self.make([self.draft.id], title="Only a draft test")
        self.login()
        self.assertEqual(self.client.get("/api/student/series/").data["results"], [])
        hidden = TestSeries.objects.get(title="Hidden")
        self.assertEqual(self.client.get(f"/api/student/series/{hidden.public_id}/").status_code, 404)

    def test_students_cannot_manage_series(self):
        self.login()
        self.assertEqual(self.client.post("/api/test-series/", {"title": "x"}, format="json").status_code, 403)


class SolutionVideoTests(EngineBase):
    def test_video_link_reaches_the_result_review(self):
        q = Question.objects.filter(exam_items__section__exam=self.exam).first()
        self.client.force_authenticate(self.admin)
        r = self.client.patch(f"/api/questions/{q.id}/", {"video_url": "https://youtu.be/abc123"}, format="json")
        self.assertEqual(r.status_code, 200, r.content)
        self.assertEqual(self.client.patch(f"/api/questions/{q.id}/", {"video_url": "not a link"}, format="json").status_code, 400)
        self.login()
        a = self.start()
        for item in self.questions(a):
            self.answer(a, item["id"], self.correct_option(item["id"]))
        self.client.post(f"/api/student/attempts/{a}/submit/")
        urls = {x["video_url"] for x in self.client.get(f"/api/student/attempts/{a}/result/").data["questions"]}
        self.assertIn("https://youtu.be/abc123", urls)

    def test_video_link_is_not_shown_while_the_attempt_is_running(self):
        self.login()
        a = self.start()
        self.assertNotIn("video_url", str(self.client.get(f"/api/student/attempts/{a}/").data))
