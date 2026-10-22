from rest_framework.test import APITestCase

from accounts.models import Role, User
from attempts.models import Attempt
from exams.models import Exam, ExamCategory, ExamQuestion, ExamSection
from questionbank.models import Question, QuestionOption, Subject

PW = "Str0ng!pass99"


def user(name, role=Role.STUDENT):
    return User.objects.create_user(username=name, email=f"{name}@x.com", password=PW, role=role)


class PublicIdTests(APITestCase):
    """Students never see or use sequential database ids, so ids cannot be guessed or counted."""

    def setUp(self):
        self.admin = user("adm", Role.ADMIN)
        self.alice = user("alice")
        self.bob = user("bob")
        subject = Subject.objects.create(name="Geo")
        self.cat = ExamCategory.objects.create(name="SSC")
        self.exam = Exam.objects.create(title="Open test", category=self.cat, status="published", duration_minutes=10, max_attempts=0)
        self.private = Exam.objects.create(title="Private test", category=self.cat, status="published", duration_minutes=10, access_type="assigned")
        for e in (self.exam, self.private):
            sec = ExamSection.objects.create(exam=e, name="S")
            q = Question.objects.create(text=f"{e.title} q", subject=subject)
            QuestionOption.objects.create(question=q, text="a", is_correct=True)
            QuestionOption.objects.create(question=q, text="b", is_correct=False)
            ExamQuestion.objects.create(section=sec, question=q, order=0, marks=1, negative_marks=0, question_version=1)

    def test_ids_are_long_random_and_unique(self):
        ids = [self.exam.public_id, self.private.public_id, self.cat.public_id]
        self.assertEqual(len(set(ids)), 3)
        for i in ids:
            self.assertGreaterEqual(len(i), 16)
            self.assertFalse(i.isdigit())
        self.client.force_authenticate(self.alice)
        a = self.client.post(f"/api/student/exams/{self.exam.public_id}/start/").data["attempt_id"]
        self.assertGreaterEqual(len(a), 16)
        self.assertFalse(a.isdigit())

    # Keys whose values identify an exam, attempt or exam category; on student endpoints they must be opaque strings.
    HANDLES = {"attempt_id", "active_attempt_id", "exam", "attempt", "category"}

    def handles_in(self, node, path=""):
        """Yields (path, value) for every protected identifier in a JSON payload."""
        if isinstance(node, dict):
            kind = "test_type" in node or "total_tests" in node or "exam_title" in node or "counts" in node or "full_tests" in node
            for k, v in node.items():
                if k in self.HANDLES and isinstance(v, dict):
                    yield f"{path}.{k}.id", v.get("id")  # nested object: its id is the handle
                elif k in self.HANDLES or (k == "id" and kind):
                    yield f"{path}.{k}", v
                yield from self.handles_in(v, f"{path}.{k}")
        elif isinstance(node, list):
            for i, v in enumerate(node):
                yield from self.handles_in(v, f"{path}[{i}]")

    def test_student_payloads_never_contain_sequential_ids(self):
        self.client.force_authenticate(self.alice)
        card = self.client.get("/api/student/exams/").data[0]
        self.assertEqual(card["id"], self.exam.public_id)
        self.assertEqual(card["category"], self.cat.public_id)
        aid = self.client.post(f"/api/student/exams/{card['id']}/start/").data["attempt_id"]
        self.client.post(f"/api/student/attempts/{aid}/submit/")
        self.client.put("/api/student/prep/", {"category_ids": [self.cat.public_id]}, format="json")

        urls = ["/api/student/exams/", f"/api/student/exams/{card['id']}/", "/api/student/attempts/", f"/api/student/attempts/{aid}/",
                f"/api/student/attempts/{aid}/result/", "/api/student/dashboard/", "/api/student/filters/",
                "/api/student/prep/", f"/api/student/prep/{self.cat.public_id}/"]
        total = 0
        for url in urls:
            r = self.client.get(url)
            self.assertEqual(r.status_code, 200, url)
            for where, value in self.handles_in(r.json()):
                if value is None:
                    continue
                total += 1
                self.assertIsInstance(value, str, f"{url} {where} = {value!r}")
                self.assertFalse(value.isdigit(), f"{url} {where} = {value!r}")
        self.assertGreater(total, 15)  # the walker really found identifiers to check

    def test_numeric_ids_no_longer_open_anything_for_students(self):
        self.client.force_authenticate(self.alice)
        aid = self.client.post(f"/api/student/exams/{self.exam.public_id}/start/").data["attempt_id"]
        real = Attempt.objects.get(public_id=aid)
        for url in (
            f"/api/student/exams/{self.exam.id}/", f"/api/student/exams/{self.exam.id}/start/",
            f"/api/student/attempts/{real.id}/", f"/api/student/attempts/{real.id}/result/",
            f"/api/student/prep/{self.cat.id}/",
        ):
            method = self.client.post if url.endswith("/start/") else self.client.get
            self.assertEqual(method(url).status_code, 404, url)
        self.assertEqual(self.client.get(f"/api/student/attempts/{aid}/").status_code, 200)

    def test_another_students_attempt_and_hidden_tests_look_identical_to_missing_ones(self):
        self.client.force_authenticate(self.alice)
        aid = self.client.post(f"/api/student/exams/{self.exam.public_id}/start/").data["attempt_id"]
        self.client.force_authenticate(self.bob)
        missing = self.client.get("/api/student/attempts/does-not-exist-123/")
        theirs = self.client.get(f"/api/student/attempts/{aid}/")
        self.assertEqual((missing.status_code, theirs.status_code), (404, 404))
        self.assertEqual(missing.content, theirs.content)  # no hint that the attempt exists
        for method, url in (("post", f"/api/student/attempts/{aid}/submit/"), ("put", f"/api/student/attempts/{aid}/answers/")):
            self.assertEqual(getattr(self.client, method)(url, {}, format="json").status_code, 404)
        hidden = self.client.get(f"/api/student/exams/{self.private.public_id}/")
        nothing = self.client.get("/api/student/exams/does-not-exist-123/")
        self.assertEqual((hidden.status_code, nothing.status_code), (404, 404))
        self.assertEqual(hidden.content, nothing.content)

    def test_duplicating_a_test_gives_the_copy_its_own_public_id(self):
        self.client.force_authenticate(self.admin)
        r = self.client.post(f"/api/exams/{self.exam.id}/duplicate/")
        self.assertEqual(r.status_code, 201, r.content)
        self.assertNotEqual(r.data["public_id"], self.exam.public_id)
        self.assertEqual(Exam.objects.values("public_id").distinct().count(), Exam.objects.count())

    def test_admins_still_manage_by_numeric_id_and_can_see_public_ids(self):
        self.client.force_authenticate(self.admin)
        r = self.client.get(f"/api/exams/{self.exam.id}/")
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.data["public_id"], self.exam.public_id)
        self.assertEqual(self.client.get(f"/api/exams/{self.exam.public_id}/").status_code, 404)
