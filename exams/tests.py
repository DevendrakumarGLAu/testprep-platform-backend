from datetime import timedelta
from unittest.mock import patch

from django.utils import timezone
from rest_framework.test import APITestCase

from accounts.models import Role, User
from questionbank.models import Question, QuestionOption, Subject

from .models import Batch, Exam, ExamQuestion

PW = "Str0ng!pass99"


def user(name, role):
    return User.objects.create_user(username=name, email=f"{name}@x.com", password=PW, role=role)


def make_question(subject, text, marks="2", neg="0.5", active=True):
    q = Question.objects.create(text=text, subject=subject, marks=marks, negative_marks=neg, is_active=active)
    QuestionOption.objects.create(question=q, text="Right", is_correct=True, order=0)
    QuestionOption.objects.create(question=q, text="Wrong", is_correct=False, order=1)
    return q


class ExamTests(APITestCase):
    def setUp(self):
        self.admin = user("adm", Role.ADMIN)
        self.student = user("stu", Role.STUDENT)
        self.student2 = user("stu2", Role.STUDENT)
        self.subject = Subject.objects.create(name="Maths")
        self.q1 = make_question(self.subject, "Question one text")
        self.q2 = make_question(self.subject, "Question two text", marks="3")
        self.client.force_authenticate(self.admin)

    def new_exam(self, **over):
        body = {"title": "Mock 1", "duration_minutes": 30, "passing_marks": "2", "max_attempts": 2}
        body.update(over)
        r = self.client.post("/api/exams/", body, format="json")
        self.assertEqual(r.status_code, 201, r.content)
        return r.data["id"]

    def set_structure(self, eid, sections=None):
        sections = sections or [
            {"name": "Section A", "questions": [{"question": self.q1.id}]},
            {"name": "Section B", "questions": [{"question": self.q2.id, "marks": "4"}]},
        ]
        return self.client.put(f"/api/exams/{eid}/structure/", {"sections": sections}, format="json")

    # --- permissions ---
    def test_students_blocked_from_all_admin_endpoints(self):
        self.client.force_authenticate(self.student)
        for url in ("/api/exams/", "/api/batches/", "/api/exam-categories/"):
            self.assertEqual(self.client.get(url).status_code, 403, url)

    # --- structure ---
    def test_structure_snapshots_marks_and_totals(self):
        eid = self.new_exam()
        r = self.set_structure(eid)
        self.assertEqual(r.status_code, 200, r.content)
        self.assertEqual([s["name"] for s in r.data["sections"]], ["Section A", "Section B"])
        self.assertEqual(r.data["question_count"], 2)
        self.assertEqual(r.data["total_marks"], "6.00")  # 2 (from bank) + 4 (override)
        # Editing the bank question later does not change the test's snapshot.
        self.q1.marks = 10
        self.q1.save()
        self.assertEqual(self.client.get(f"/api/exams/{eid}/").data["total_marks"], "6.00")

    def test_structure_rejects_duplicates_and_inactive_questions(self):
        eid = self.new_exam()
        dup = [{"name": "A", "questions": [{"question": self.q1.id}, {"question": self.q1.id}]}]
        self.assertEqual(self.set_structure(eid, dup).status_code, 400)
        inactive = make_question(self.subject, "Archived one", active=False)
        bad = [{"name": "A", "questions": [{"question": inactive.id}]}]
        self.assertEqual(self.set_structure(eid, bad).status_code, 400)
        same_names = [{"name": "A", "questions": []}, {"name": "a", "questions": []}]
        self.assertEqual(self.set_structure(eid, same_names).status_code, 400)

    # --- publish rules ---
    def test_cannot_publish_empty_or_invalid(self):
        eid = self.new_exam()
        r = self.client.post(f"/api/exams/{eid}/publish/")
        self.assertEqual(r.status_code, 400)
        self.assertIn("Add at least one question.", r.data["errors"])

        self.set_structure(eid)
        self.client.patch(f"/api/exams/{eid}/", {"passing_marks": "99"}, format="json")
        r = self.client.post(f"/api/exams/{eid}/publish/")
        self.assertTrue(any("exceed total marks" in e for e in r.data["errors"]))

    def test_assigned_access_needs_assignees_and_window_must_fit_duration(self):
        eid = self.new_exam(access_type="assigned")
        self.set_structure(eid)
        r = self.client.post(f"/api/exams/{eid}/publish/")
        self.assertTrue(any("Assign at least one student" in e for e in r.data["errors"]))

        now = timezone.now()
        self.client.patch(
            f"/api/exams/{eid}/",
            {"start_at": (now + timedelta(hours=1)).isoformat(), "end_at": (now + timedelta(hours=1, minutes=10)).isoformat()},
            format="json",
        )
        r = self.client.post(f"/api/exams/{eid}/publish/")
        self.assertTrue(any("shorter than the test duration" in e for e in r.data["errors"]))

    def test_publish_lock_unpublish_and_restore(self):
        eid = self.new_exam()
        self.set_structure(eid)
        r = self.client.post(f"/api/exams/{eid}/publish/")
        self.assertEqual(r.status_code, 200, r.content)
        self.assertEqual(r.data["status"], "published")
        self.assertEqual(r.data["version"], 2)

        # Scoring rules and questions are frozen; descriptive fields are not.
        r = self.client.patch(f"/api/exams/{eid}/", {"duration_minutes": 45}, format="json")
        self.assertEqual(r.status_code, 400)
        self.assertIn("duration_minutes", r.data)
        self.assertEqual(self.client.patch(f"/api/exams/{eid}/", {"title": "Renamed"}, format="json").status_code, 200)
        self.assertEqual(self.set_structure(eid).status_code, 400)
        # Unchanged locked values in a full PUT are tolerated.
        data = self.client.get(f"/api/exams/{eid}/").data
        self.assertEqual(self.client.patch(f"/api/exams/{eid}/", {"duration_minutes": data["duration_minutes"]}, format="json").status_code, 200)

        self.assertEqual(self.client.delete(f"/api/exams/{eid}/").status_code, 409)
        self.assertEqual(self.client.post(f"/api/exams/{eid}/unpublish/").data["status"], "draft")
        self.assertEqual(self.client.patch(f"/api/exams/{eid}/", {"duration_minutes": 45}, format="json").status_code, 200)

        self.client.post(f"/api/exams/{eid}/archive/")
        self.assertEqual(self.client.patch(f"/api/exams/{eid}/", {"title": "x"}, format="json").status_code, 400)
        self.assertEqual(self.client.post(f"/api/exams/{eid}/restore/").data["status"], "draft")

    def test_unpublish_blocked_once_attempted(self):
        eid = self.new_exam()
        self.set_structure(eid)
        self.client.post(f"/api/exams/{eid}/publish/")
        with patch.object(Exam, "has_attempts", return_value=True):  # engine hook, wired up in a later phase
            self.assertEqual(self.client.post(f"/api/exams/{eid}/unpublish/").status_code, 400)

    def test_draft_can_be_deleted(self):
        eid = self.new_exam()
        self.assertEqual(self.client.delete(f"/api/exams/{eid}/").status_code, 204)

    # --- validation ---
    def test_field_validation(self):
        self.assertEqual(self.client.post("/api/exams/", {"title": "x", "duration_minutes": 0}, format="json").status_code, 400)
        now = timezone.now()
        r = self.client.post("/api/exams/", {
            "title": "x", "start_at": now.isoformat(), "end_at": (now - timedelta(hours=1)).isoformat(),
        }, format="json")
        self.assertIn("end_at", r.data)
        r = self.client.post("/api/exams/", {"title": "x", "result_release": "scheduled"}, format="json")
        self.assertIn("result_release_at", r.data)

    # --- assignment / batches ---
    def test_assignment_and_batches(self):
        eid = self.new_exam(access_type="batch")
        b = self.client.post("/api/batches/", {"name": "Batch A"}, format="json").data["id"]
        r = self.client.post(f"/api/batches/{b}/add-members/", {"student_ids": [self.student.id, self.admin.id]}, format="json")
        self.assertEqual(r.data["member_count"], 1)  # the admin is not a student and is ignored
        r = self.client.put(f"/api/exams/{eid}/assignment/", {"batches": [b], "students": [self.student2.id]}, format="json")
        self.assertEqual(r.data["assigned_batch_ids"], [b])
        self.assertEqual(r.data["assigned_student_ids"], [self.student2.id])
        # A batch used by a test cannot be deleted.
        self.assertEqual(self.client.delete(f"/api/batches/{b}/").status_code, 409)
        self.assertEqual([m["id"] for m in self.client.get(f"/api/batches/{b}/members/").data], [self.student.id])
        self.client.post(f"/api/batches/{b}/remove-members/", {"student_ids": [self.student.id]}, format="json")
        self.assertEqual(Batch.objects.get(pk=b).students.count(), 0)
        # Non-students cannot be assigned.
        r = self.client.put(f"/api/exams/{eid}/assignment/", {"students": [self.admin.id]}, format="json")
        self.assertEqual(r.status_code, 400)

    def test_batch_name_unique(self):
        self.client.post("/api/batches/", {"name": "B"}, format="json")
        self.assertEqual(self.client.post("/api/batches/", {"name": "b"}, format="json").status_code, 400)

    # --- duplicate ---
    def test_duplicate_makes_independent_draft(self):
        eid = self.new_exam()
        self.set_structure(eid)
        self.client.post(f"/api/exams/{eid}/publish/")
        r = self.client.post(f"/api/exams/{eid}/duplicate/")
        self.assertEqual(r.status_code, 201)
        self.assertEqual(r.data["status"], "draft")
        self.assertEqual(r.data["title"], "Mock 1 (copy)")
        self.assertEqual(r.data["question_count"], 2)
        self.assertEqual(ExamQuestion.objects.count(), 4)

    # --- preview ---
    def test_preview_hides_answers(self):
        eid = self.new_exam()
        self.set_structure(eid)
        r = self.client.get(f"/api/exams/{eid}/preview/")
        self.assertEqual(r.status_code, 200)
        q = r.data["sections"][0]["questions"][0]
        self.assertNotIn("is_correct", q["options"][0])
        self.assertNotIn("explanation", q)

    # --- filters ---
    def test_list_filters(self):
        a = self.new_exam(title="Alpha")
        self.new_exam(title="Beta")
        self.set_structure(a)
        self.client.post(f"/api/exams/{a}/publish/")
        self.assertEqual(self.client.get("/api/exams/?status=published").data["count"], 1)
        self.assertEqual(self.client.get("/api/exams/?search=beta").data["count"], 1)

    def test_question_in_exam_cannot_be_hard_deleted(self):
        eid = self.new_exam()
        self.set_structure(eid)
        from django.db.models import ProtectedError
        with self.assertRaises(ProtectedError):
            self.q1.delete()
