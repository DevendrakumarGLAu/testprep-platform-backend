"""
Cross-cutting tests: authorisation for every API route, query-count guards and one full end-to-end journey.
"""
import io
import re

from django.db import connection
from django.test.utils import CaptureQueriesContext
from django.urls import URLPattern, URLResolver, get_resolver
from pypdf import PdfReader
from rest_framework.test import APIClient, APITestCase

from accounts.models import ActivityLog, Role, User
from exams.models import Exam, ExamQuestion, ExamSection
from questionbank.models import Question, QuestionOption, Subject

PW = "Str0ng!pass99"
METHODS = ("get", "post", "put", "patch", "delete")

# Routes any anonymous visitor may call (they handle their own checks).
PUBLIC = {"api/auth/login/", "api/auth/refresh/", "api/auth/register/", "api/auth/logout/", "api/platform/"}
# Routes any signed-in user may call, whatever their role.
ANY_ROLE = {"api/auth/me/", "api/auth/change-password/"}


def api_routes():
    """Every concrete URL under /api/, with path parameters filled in."""

    def walk(patterns, prefix=""):
        for p in patterns:
            route = prefix + str(p.pattern)
            if isinstance(p, URLResolver):
                yield from walk(p.url_patterns, route)
            elif isinstance(p, URLPattern):
                yield route

    seen = set()
    for raw in walk(get_resolver().url_patterns):
        if "format" in raw:  # DRF's optional .json suffix routes
            continue
        route = raw.replace("^", "").replace("$", "").replace("\\.", ".")
        route = re.sub(r"\(\?P<\w+>[^)]*\)", "1", route)
        route = re.sub(r"<\w+:\w+>", "1", route)
        route = re.sub(r"<\w+>", "1", route).replace("/?", "/")
        if route.startswith("api/") and route != "api/":
            seen.add(route)
    return sorted(seen)


def make(name, role):
    return User.objects.create_user(username=name, email=f"{name}@x.com", password=PW, role=role, first_name=name.title())


class PermissionMatrixTests(APITestCase):
    """If someone adds an endpoint and forgets its permission class, one of these fails."""

    @classmethod
    def setUpTestData(cls):
        cls.routes = api_routes()
        cls.student = make("stu", Role.STUDENT)
        cls.admin = make("adm", Role.ADMIN)
        cls.boss = make("boss", Role.SUPER_ADMIN)

    def hit(self, user, route, method):
        self.client.force_authenticate(user)
        return getattr(self.client, method)(f"/{route}", {}, format="json")

    def test_route_discovery_finds_the_whole_api(self):
        self.assertGreater(len(self.routes), 60)
        for expected in ("api/exams/1/publish/", "api/student/attempts/1/answers/", "api/imports/1/commit/",
                         "api/students/1/report.pdf", "api/reports/consolidated.pdf", "api/admin/search/"):
            self.assertIn(expected, self.routes)

    def test_anonymous_users_are_rejected_everywhere_that_is_not_public(self):
        checked = 0
        for route in self.routes:
            if route in PUBLIC:
                continue
            for m in METHODS:
                r = self.hit(None, route, m)
                self.assertEqual(r.status_code, 401, f"anonymous {m.upper()} /{route} -> {r.status_code}")
                checked += 1
        self.assertGreater(checked, 300)

    def test_students_are_rejected_from_every_admin_route(self):
        for route in self.routes:
            if route in PUBLIC or route in ANY_ROLE or route.startswith("api/student/"):
                continue
            for m in METHODS:
                r = self.hit(self.student, route, m)
                self.assertEqual(r.status_code, 403, f"student {m.upper()} /{route} -> {r.status_code}")

    def test_admins_and_super_admins_cannot_use_student_routes(self):
        for who in (self.admin, self.boss):
            for route in self.routes:
                if not route.startswith("api/student/"):
                    continue
                for m in METHODS:
                    r = self.hit(who, route, m)
                    self.assertEqual(r.status_code, 403, f"{who.role} {m.upper()} /{route} -> {r.status_code}")

    def test_super_admin_only_routes_reject_plain_admins(self):
        self.assertEqual(self.hit(self.admin, "api/activity-logs/", "get").status_code, 403)
        self.assertEqual(self.hit(self.admin, "api/platform/", "patch").status_code, 403)
        self.assertEqual(self.hit(self.boss, "api/activity-logs/", "get").status_code, 200)

    def test_unsupported_methods_never_cause_server_errors(self):
        """With valid credentials, odd methods and empty bodies should give 4xx, never a 500."""
        for who in (self.admin, self.student):
            self.client.force_authenticate(who)
            for route in self.routes:
                if route.endswith(".pdf"):
                    continue
                for m in METHODS:
                    r = getattr(self.client, m)(f"/{route}", {}, format="json")
                    self.assertLess(r.status_code, 500, f"{who.role} {m.upper()} /{route} -> {r.status_code}")


class QueryCountTests(APITestCase):
    """List pages must not run a query per row."""

    def setUp(self):
        self.admin = make("adm", Role.ADMIN)
        self.student = make("stu", Role.STUDENT)
        self.subject = Subject.objects.create(name="Geo")
        for i in range(12):
            exam = Exam.objects.create(title=f"Test {i}", status="published", duration_minutes=30)
            sec = ExamSection.objects.create(exam=exam, name="S")
            for j in range(3):
                q = Question.objects.create(text=f"Q {i}-{j}", subject=self.subject)
                QuestionOption.objects.create(question=q, text="a", is_correct=True)
                QuestionOption.objects.create(question=q, text="b", is_correct=False)
                ExamQuestion.objects.create(section=sec, question=q, order=j, marks=2, negative_marks=0, question_version=1)

    def count(self, user, url):
        self.client.force_authenticate(user)
        with CaptureQueriesContext(connection) as ctx:
            r = self.client.get(url)
        self.assertEqual(r.status_code, 200)
        return len(ctx), r

    def test_admin_exam_list_is_constant_in_queries(self):
        n, r = self.count(self.admin, "/api/exams/?page_size=50")
        self.assertEqual(len(r.data["results"]), 12)
        self.assertEqual(r.data["results"][0]["question_count"], 3)
        self.assertEqual(r.data["results"][0]["total_marks"], "6.00")
        self.assertLess(n, 12, f"{n} queries for 12 tests")

    def test_student_exam_list_is_constant_in_queries(self):
        n, r = self.count(self.student, "/api/student/exams/")
        self.assertEqual(len(r.data), 12)
        self.assertEqual((r.data[0]["question_count"], r.data[0]["total_marks"]), (3, "6.00"))
        self.assertLess(n, 15, f"{n} queries for 12 tests")

    def test_totals_are_right_when_assignments_would_multiply_joined_rows(self):
        from exams.models import Batch

        exam = Exam.objects.get(title="Test 0")
        exam.access_type = "batch"
        exam.save()
        for k in range(3):
            b = Batch.objects.create(name=f"B{k}")
            b.students.add(self.student)
            exam.assigned_batches.add(b)
        self.client.force_authenticate(self.student)
        card = next(c for c in self.client.get("/api/student/exams/").data if c["title"] == "Test 0")
        self.assertEqual((card["question_count"], card["total_marks"]), (3, "6.00"))

    def test_question_and_student_lists(self):
        for i in range(10):
            make(f"s{i}", Role.STUDENT)
        n, _ = self.count(self.admin, "/api/questions/?page_size=50")
        self.assertLess(n, 10, f"{n} queries for the question list")
        n, r = self.count(self.admin, "/api/students/?page_size=50")
        self.assertEqual(r.data["count"], 11)
        self.assertLess(n, 10, f"{n} queries for the student list")


class SearchTests(APITestCase):
    def setUp(self):
        self.admin = make("adm", Role.ADMIN)
        self.student = make("stu", Role.STUDENT)
        make("zebra", Role.STUDENT)
        subject = Subject.objects.create(name="Geo")
        Question.objects.create(text="Which river runs through Cairo?", subject=subject)
        Exam.objects.create(title="River systems mock")
        self.client.force_authenticate(self.admin)

    def test_finds_across_types(self):
        d = self.client.get("/api/admin/search/?q=river").data
        self.assertEqual((len(d["questions"]), len(d["tests"]), len(d["students"])), (1, 1, 0))
        d = self.client.get("/api/admin/search/?q=zebra").data
        self.assertEqual([s["name"] for s in d["students"]], ["Zebra"])

    def test_short_queries_return_nothing_and_admins_are_not_listed_as_students(self):
        self.assertEqual(self.client.get("/api/admin/search/?q=r").data, {"questions": [], "tests": [], "students": []})
        self.assertEqual(self.client.get("/api/admin/search/?q=adm").data["students"], [])

    def test_students_cannot_search(self):
        self.client.force_authenticate(self.student)
        self.assertEqual(self.client.get("/api/admin/search/?q=river").status_code, 403)


class FullJourneyTests(APITestCase):
    """Admin builds a test; a student signs up, takes it and sees the result; admin reviews everything."""

    def login(self, username):
        c = APIClient()
        r = c.post("/api/auth/login/", {"username": username, "password": PW}, format="json")
        self.assertEqual(r.status_code, 200, r.content)
        c.credentials(HTTP_AUTHORIZATION=f"Bearer {r.data['access']}")
        return c, r.data

    def test_end_to_end(self):
        make("boss", Role.SUPER_ADMIN)
        make("teacher", Role.ADMIN)
        admin, _ = self.login("teacher")

        # --- admin builds content ---
        subject = admin.post("/api/subjects/", {"name": "Geography"}, format="json").data
        topic = admin.post("/api/topics/", {"subject": subject["id"], "name": "Capitals"}, format="json").data
        qids = []
        for i, text in enumerate(["Capital of France?", "Capital of Italy?", "Capital of Spain?"]):
            r = admin.post("/api/questions/", {
                "text": text, "subject": subject["id"], "topic": topic["id"], "difficulty": "easy",
                "marks": "2", "negative_marks": "1", "explanation": f"Because {i}",
                "options": [{"text": "Right", "is_correct": True}, {"text": "Wrong", "is_correct": False}],
            }, format="json")
            self.assertEqual(r.status_code, 201, r.content)
            qids.append(r.data["id"])
        exam = admin.post("/api/exams/", {
            "title": "Capitals quiz", "duration_minutes": 20, "passing_marks": "3", "max_attempts": 0,
        }, format="json").data
        r = admin.put(f"/api/exams/{exam['id']}/structure/", {"sections": [
            {"name": "Europe", "questions": [{"question": q} for q in qids]},
        ]}, format="json")
        self.assertEqual(r.data["total_marks"], "6.00")
        self.assertEqual(admin.post(f"/api/exams/{exam['id']}/publish/").status_code, 200)

        # --- student signs up, signs in, takes the test ---
        r = APIClient().post("/api/auth/register/", {
            "username": "newbie", "email": "newbie@x.com", "password": PW, "first_name": "Nina", "last_name": "Newbie",
        }, format="json")
        self.assertEqual(r.status_code, 201)
        student, _ = self.login("newbie")
        self.assertEqual(student.get("/api/exams/").status_code, 403)  # no admin access

        cards = student.get("/api/student/exams/").data
        self.assertEqual([c["title"] for c in cards], ["Capitals quiz"])
        self.assertEqual(cards[0]["status"], "available")
        start = student.post(f"/api/student/exams/{exam['public_id']}/start/")
        self.assertEqual(start.status_code, 201)
        aid = start.data["attempt_id"]
        attempt = student.get(f"/api/student/attempts/{aid}/").data
        self.assertNotIn("is_correct", str(attempt))
        questions = attempt["sections"][0]["questions"]
        for i, q in enumerate(questions):
            want = "Right" if i < 2 else "Wrong"  # two correct, one wrong: +2 +2 -1 = 3
            opt = next(o["id"] for o in q["options"] if o["text"] == want)
            r = student.put(f"/api/student/attempts/{aid}/answers/", {"question": q["id"], "selected_option": opt}, format="json")
            self.assertEqual(r.status_code, 200)
        done = student.post(f"/api/student/attempts/{aid}/submit/").data
        self.assertEqual(done["result"]["score"], "3.00")
        self.assertTrue(done["result"]["passed"])
        self.assertEqual(student.post(f"/api/student/attempts/{aid}/submit/").data["result"], done["result"])  # idempotent

        review = student.get(f"/api/student/attempts/{aid}/result/").data
        self.assertEqual([q["status"] for q in review["questions"]], ["correct", "correct", "incorrect"])
        self.assertEqual(student.get("/api/student/dashboard/").data["counts"]["completed"], 1)
        # A second attempt is allowed (unlimited retakes).
        self.assertEqual(student.post(f"/api/student/exams/{exam['public_id']}/start/").status_code, 201)

        # --- admin sees the outcome ---
        summary = admin.get(f"/api/exams/{exam['id']}/results-summary/").data
        self.assertEqual((summary["attempts_submitted"], summary["highest_score"]), (1, "3.00"))
        self.assertEqual(admin.get(f"/api/exams/{exam['id']}/attempts/").data["count"], 2)
        dash = admin.get("/api/admin/dashboard/").data
        self.assertEqual((dash["students"]["total"], dash["tests"]["published"], dash["questions"]["total"]), (1, 1, 3))
        students = admin.get("/api/students/?search=nina").data
        sid = students["results"][0]["id"]
        profile = admin.get(f"/api/students/{sid}/").data
        self.assertEqual(profile["performance"]["attempts_count"], 1)
        pdf = admin.get(f"/api/students/{sid}/report.pdf")
        self.assertEqual(pdf["Content-Type"], "application/pdf")
        text = "\n".join(p.extract_text() for p in PdfReader(io.BytesIO(pdf.content)).pages)
        self.assertIn("Nina Newbie", text)
        self.assertIn("Capitals quiz", text)
        consolidated = admin.post("/api/reports/consolidated.pdf", {"exam_ids": [exam["id"]]}, format="json")
        self.assertTrue(consolidated.content.startswith(b"%PDF"))
        self.assertEqual(admin.get("/api/admin/search/?q=capital").data["tests"][0]["title"], "Capitals quiz")

        # The published test is frozen now that a student attempted it.
        self.assertEqual(admin.post(f"/api/exams/{exam['id']}/unpublish/").status_code, 400)

        # --- super admin sees the audit trail; the plain admin does not ---
        self.assertEqual(admin.get("/api/activity-logs/").status_code, 403)
        boss, _ = self.login("boss")
        actions = {a["action"] for a in boss.get("/api/activity-logs/?page_size=100").data["results"]}
        for expected in ("subject.create", "question.create", "exam.create", "exam.publish", "report.student_pdf"):
            self.assertIn(expected, actions)
        self.assertTrue(ActivityLog.objects.filter(actor__username="teacher").exists())


class StableOrderingTests(APITestCase):
    """Annotated list queries drop Meta.ordering, so views must order explicitly or pages can repeat/skip rows."""

    def test_annotated_lists_are_ordered_and_paginate_without_overlap(self):
        import warnings

        from exams.models import Batch, ExamCategory
        from questionbank.models import Topic

        admin = make("adm", Role.ADMIN)
        subject = Subject.objects.create(name="A subject")
        for i in range(7):
            Subject.objects.create(name=f"S{i}")
            Batch.objects.create(name=f"B{i}")
            ExamCategory.objects.create(name=f"C{i}")
            Topic.objects.create(subject=subject, name=f"T{i}")
        self.client.force_authenticate(admin)
        with warnings.catch_warnings():
            warnings.simplefilter("error")  # an UnorderedObjectListWarning would fail the test
            for url in ("subjects", "batches", "exam-categories", "topics"):
                page1 = self.client.get(f"/api/{url}/?page_size=4&page=1").data
                page2 = self.client.get(f"/api/{url}/?page_size=4&page=2").data
                ids = [r["id"] for r in page1["results"] + page2["results"]]
                self.assertEqual(len(ids), len(set(ids)), url)
                self.assertEqual(len(ids), min(8, page1["count"]), url)
