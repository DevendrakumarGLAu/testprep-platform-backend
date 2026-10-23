from questionbank.models import Question, QuestionReport

from .models import SavedQuestion
from .tests import EngineBase


class ReviewBase(EngineBase):
    def setUp(self):
        super().setUp()
        self.login()
        self.a = self.start()
        for q in self.questions(self.a):
            self.answer(self.a, q["id"], self.wrong_option(q["id"]))
        self.client.post(f"/api/student/attempts/{self.a}/submit/")
        self.url = lambda n, what: f"/api/student/attempts/{self.a}/questions/{n}/{what}/"


class SaveQuestionTests(ReviewBase):
    def test_save_copies_the_question_with_its_answer(self):
        r = self.client.post(self.url(1, "save"), {"note": "revise this"}, format="json")
        self.assertEqual(r.status_code, 201, r.content)
        self.assertEqual(r.data["note"], "revise this")
        self.assertTrue(any(o["is_correct"] for o in r.data["options"]))
        self.assertEqual(r.data["explanation"], "because")
        saved = self.client.get("/api/student/saved/")
        self.assertEqual(saved.data["count"], 1)
        self.assertEqual(saved.data["results"][0]["exam_title"], "Mock")

    def test_saving_twice_updates_the_note_instead_of_duplicating(self):
        self.client.post(self.url(1, "save"), {"note": "one"}, format="json")
        r = self.client.post(self.url(1, "save"), {"note": "two"}, format="json")
        self.assertEqual(r.status_code, 200)
        self.assertEqual(SavedQuestion.objects.count(), 1)
        self.assertEqual(SavedQuestion.objects.get().note, "two")

    def test_note_can_be_edited_and_the_question_removed(self):
        sid = self.client.post(self.url(1, "save"), {}, format="json").data["id"]
        self.assertEqual(self.client.patch(f"/api/student/saved/{sid}/", {"note": "later"}, format="json").data["note"], "later")
        self.assertEqual(self.client.delete(f"/api/student/saved/{sid}/").status_code, 204)
        self.assertEqual(self.client.get("/api/student/saved/").data["count"], 0)

    def test_unsave_from_the_review(self):
        self.client.post(self.url(2, "save"), {}, format="json")
        self.assertEqual(self.client.delete(self.url(2, "save")).status_code, 204)
        self.assertEqual(SavedQuestion.objects.count(), 0)

    def test_only_my_own_saved_questions_are_visible_or_editable(self):
        sid = self.client.post(self.url(1, "save"), {}, format="json").data["id"]
        self.client.force_authenticate(self.other)
        self.assertEqual(self.client.get("/api/student/saved/").data["count"], 0)
        self.assertEqual(self.client.patch(f"/api/student/saved/{sid}/", {"note": "x"}, format="json").status_code, 404)
        self.assertEqual(self.client.delete(f"/api/student/saved/{sid}/").status_code, 404)
        self.assertEqual(self.client.post(self.url(1, "save"), {}, format="json").status_code, 404)  # not their attempt

    def test_cannot_save_before_the_test_is_submitted(self):
        b = self.start(self.make_exam(n=2))
        r = self.client.post(f"/api/student/attempts/{b}/questions/1/save/", {}, format="json")
        self.assertEqual(r.status_code, 400)

    def test_note_length_is_limited(self):
        self.assertEqual(self.client.post(self.url(1, "save"), {"note": "x" * 1001}, format="json").status_code, 400)

    def test_unknown_question_number(self):
        self.assertEqual(self.client.post(self.url(99, "save"), {}, format="json").status_code, 404)

    def test_the_result_review_says_which_questions_are_saved_or_reported(self):
        self.client.post(self.url(1, "save"), {}, format="json")
        self.client.post(self.url(2, "report"), {"reason": "typo"}, format="json")
        qs = self.client.get(f"/api/student/attempts/{self.a}/result/").data["questions"]
        self.assertIsNotNone(qs[0]["saved_id"])
        self.assertIsNone(qs[0]["report_status"])
        self.assertIsNone(qs[1]["saved_id"])
        self.assertEqual(qs[1]["report_status"], "open")


class ReportQuestionTests(ReviewBase):
    def test_a_report_is_stored_and_the_question_is_flagged_for_review(self):
        r = self.client.post(self.url(1, "report"), {"reason": "wrong_answer", "message": "Option B is right"}, format="json")
        self.assertEqual(r.status_code, 201, r.content)
        report = QuestionReport.objects.get()
        self.assertEqual((report.reason, report.status, report.student), ("wrong_answer", "open", self.stu))
        self.assertEqual(report.exam_title, "Mock")
        self.assertEqual(report.question.review_status, "needs_review")

    def test_reporting_again_updates_the_open_report(self):
        self.client.post(self.url(1, "report"), {"reason": "typo"}, format="json")
        r = self.client.post(self.url(1, "report"), {"reason": "wrong_answer", "message": "now I think it is the answer"}, format="json")
        self.assertEqual(r.status_code, 200)
        self.assertTrue(r.data["updated"])
        self.assertEqual(QuestionReport.objects.count(), 1)
        self.assertEqual(QuestionReport.objects.get().reason, "wrong_answer")

    def test_a_reason_is_required_and_other_needs_words(self):
        self.assertEqual(self.client.post(self.url(1, "report"), {}, format="json").status_code, 400)
        self.assertEqual(self.client.post(self.url(1, "report"), {"reason": "nonsense"}, format="json").status_code, 400)
        self.assertEqual(self.client.post(self.url(1, "report"), {"reason": "other", "message": "hi"}, format="json").status_code, 400)
        self.assertEqual(self.client.post(self.url(1, "report"), {"reason": "other", "message": "This does not make sense"}, format="json").status_code, 201)

    def test_only_after_the_test_and_only_on_my_own_attempt(self):
        b = self.start(self.make_exam(n=2))
        self.assertEqual(self.client.post(f"/api/student/attempts/{b}/questions/1/report/", {"reason": "typo"}, format="json").status_code, 400)
        self.client.force_authenticate(self.other)
        self.assertEqual(self.client.post(self.url(1, "report"), {"reason": "typo"}, format="json").status_code, 404)

    def test_a_student_sees_their_reports_and_the_answer(self):
        self.client.post(self.url(1, "report"), {"reason": "typo"}, format="json")
        mine = self.client.get("/api/student/reports/").data["results"]
        self.assertEqual((len(mine), mine[0]["status"], mine[0]["reason"]), (1, "open", "Spelling or typing mistake"))
        self.client.force_authenticate(self.other)
        self.assertEqual(self.client.get("/api/student/reports/").data["results"], [])


class AdminReportQueueTests(ReviewBase):
    def setUp(self):
        super().setUp()
        self.client.post(self.url(1, "report"), {"reason": "wrong_answer", "message": "B is right"}, format="json")
        self.report = QuestionReport.objects.get()

    def as_admin(self):
        self.client.force_authenticate(self.admin)

    def test_students_cannot_open_the_queue(self):
        self.assertEqual(self.client.get("/api/question-reports/").status_code, 403)

    def test_admin_sees_and_filters_the_queue(self):
        self.as_admin()
        r = self.client.get("/api/question-reports/?status=open")
        self.assertEqual(r.data["count"], 1)
        row = r.data["results"][0]
        self.assertEqual((row["reason_label"], row["message"], row["exam_title"]), ("The marked answer is wrong", "B is right", "Mock"))
        self.assertEqual(self.client.get("/api/question-reports/?status=resolved").data["count"], 0)

    def test_resolving_clears_the_question_and_keeps_the_note(self):
        self.as_admin()
        r = self.client.patch(f"/api/question-reports/{self.report.id}/", {"status": "resolved", "admin_note": "Fixed the key"}, format="json")
        self.assertEqual(r.status_code, 200, r.content)
        self.report.refresh_from_db()
        self.assertEqual((self.report.status, self.report.admin_note, self.report.resolved_by), ("resolved", "Fixed the key", self.admin))
        self.assertEqual(self.report.question.review_status, "approved")
        self.client.force_authenticate(self.stu)
        mine = self.client.get("/api/student/reports/").data["results"][0]
        self.assertEqual((mine["status"], mine["admin_note"]), ("resolved", "Fixed the key"))

    def test_question_stays_flagged_while_another_report_is_open(self):
        self.client.force_authenticate(self.other)
        # the other student takes the same test and reports the same question
        b = self.start()
        for q in self.questions(b):
            self.answer(b, q["id"], self.wrong_option(q["id"]))
        self.client.post(f"/api/student/attempts/{b}/submit/")
        self.client.post(f"/api/student/attempts/{b}/questions/1/report/", {"reason": "typo"}, format="json")
        self.as_admin()
        self.client.patch(f"/api/question-reports/{self.report.id}/", {"status": "resolved"}, format="json")
        self.assertEqual(Question.objects.get(pk=self.report.question_id).review_status, "needs_review")

    def test_reports_cannot_be_deleted_or_created_here(self):
        self.as_admin()
        self.assertEqual(self.client.delete(f"/api/question-reports/{self.report.id}/").status_code, 405)
        self.assertEqual(self.client.post("/api/question-reports/", {}, format="json").status_code, 405)

    def test_questions_can_be_filtered_by_review_state_and_report(self):
        self.as_admin()
        self.assertEqual(self.client.get("/api/questions/?review_status=needs_review").data["count"], 1)
        self.assertEqual(self.client.get("/api/questions/?reported=true").data["count"], 1)
        row = self.client.get("/api/questions/?reported=true").data["results"][0]
        self.assertEqual((row["review_status"], row["open_reports"]), ("needs_review", 1))
        self.client.patch(f"/api/question-reports/{self.report.id}/", {"status": "rejected"}, format="json")
        self.assertEqual(self.client.get("/api/questions/?reported=true").data["count"], 0)

    def test_bulk_approve(self):
        self.as_admin()
        qid = self.report.question_id
        r = self.client.post("/api/questions/bulk/", {"ids": [qid], "action": "set_review_status", "review_status": "approved"}, format="json")
        self.assertEqual(r.status_code, 200, r.content)
        self.assertEqual(Question.objects.get(pk=qid).review_status, "approved")

    def test_a_test_with_a_question_under_review_cannot_be_published(self):
        from exams.models import Exam
        self.as_admin()
        draft = self.make_exam(publish=False)
        flagged = Question.objects.filter(exam_items__section__exam=draft).first()
        flagged.review_status = "needs_review"
        flagged.save()
        r = self.client.post(f"/api/exams/{draft.id}/publish/")
        self.assertEqual(r.status_code, 400)
        self.assertIn("needs review", str(r.data))
        flagged.review_status = "approved"
        flagged.save()
        self.assertEqual(self.client.post(f"/api/exams/{draft.id}/publish/").status_code, 200)
        self.assertEqual(Exam.objects.get(pk=draft.id).status, "published")
