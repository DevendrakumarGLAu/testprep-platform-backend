import datetime

from django.utils import timezone
from rest_framework.test import APITestCase

from accounts.models import Role, User

from .models import Question, QuestionOption, Subject

PW = "Str0ng!pass99"


def years_ago(n):
    return timezone.localdate() - datetime.timedelta(days=365 * n)


class QuestionDateTests(APITestCase):
    def setUp(self):
        self.admin = User.objects.create_user(username="adm", email="a@x.com", password=PW, role=Role.ADMIN)
        self.subject = Subject.objects.create(name="Current Affairs")
        self.client.force_authenticate(self.admin)

    def body(self, text="Q?", **extra):
        return {"text": text, "subject": self.subject.id,
                "options": [{"text": "a", "is_correct": True}, {"text": "b", "is_correct": False}], **extra}

    def make(self, text, date):
        q = Question.objects.create(text=text, subject=self.subject, question_date=date)
        QuestionOption.objects.create(question=q, text="a", is_correct=True, order=0)
        QuestionOption.objects.create(question=q, text="b", is_correct=False, order=1)
        return q

    def test_date_is_saved_and_returned(self):
        r = self.client.post("/api/questions/", self.body(question_date="2026-07-08"), format="json")
        self.assertEqual(r.status_code, 201, r.content)
        self.assertEqual(r.data["question_date"], "2026-07-08")

    def test_defaults_to_today_when_not_given(self):
        r = self.client.post("/api/questions/", self.body(), format="json")
        self.assertEqual(r.data["question_date"], timezone.localdate().isoformat())

    def test_future_date_is_refused(self):
        tomorrow = (timezone.localdate() + datetime.timedelta(days=1)).isoformat()
        r = self.client.post("/api/questions/", self.body(question_date=tomorrow), format="json")
        self.assertEqual(r.status_code, 400)
        self.assertIn("question_date", r.data)

    def test_date_can_be_changed_and_cleared(self):
        q = self.make("Old", years_ago(3))
        r = self.client.patch(f"/api/questions/{q.id}/", {"question_date": "2026-01-02"}, format="json")
        self.assertEqual(r.data["question_date"], "2026-01-02")
        r = self.client.patch(f"/api/questions/{q.id}/", {"question_date": None}, format="json")
        self.assertIsNone(r.data["question_date"])

    def test_changing_only_the_date_does_not_bump_the_version(self):
        q = self.make("Same", years_ago(1))
        self.client.patch(f"/api/questions/{q.id}/", {"question_date": "2026-01-02"}, format="json")
        q.refresh_from_db()
        self.assertEqual(q.version, 1)

    def test_list_filters_by_date_range(self):
        old, mid, new = self.make("old", years_ago(3)), self.make("mid", years_ago(1)), self.make("new", timezone.localdate())
        since = years_ago(2).isoformat()
        ids = lambda r: sorted(x["id"] for x in r.data["results"])  # noqa: E731
        self.assertEqual(ids(self.client.get(f"/api/questions/?date_from={since}")), sorted([mid.id, new.id]))
        self.assertEqual(ids(self.client.get(f"/api/questions/?date_to={since}")), [old.id])

    def test_list_can_be_sorted_by_date(self):
        a, b = self.make("a", years_ago(2)), self.make("b", years_ago(1))
        r = self.client.get("/api/questions/?ordering=-question_date")
        self.assertEqual([x["id"] for x in r.data["results"]][:2], [b.id, a.id])

    def test_pick_respects_the_date_window(self):
        self.make("old", years_ago(4))
        recent = self.make("recent", years_ago(1))
        r = self.client.post("/api/questions/pick/", {"count": 10, "mode": "newest", "date_from": years_ago(2).isoformat()}, format="json")
        self.assertEqual([q["id"] for q in r.data["questions"]], [recent.id])
        self.assertEqual(str(r.data["questions"][0]["question_date"]), years_ago(1).isoformat())
        bad = self.client.post("/api/questions/pick/", {"count": 1, "mode": "newest", "date_from": "last year"}, format="json")
        self.assertEqual(bad.status_code, 400)

    def test_duplicate_keeps_the_date(self):
        q = self.make("Dup", years_ago(1))
        r = self.client.post(f"/api/questions/{q.id}/duplicate/")
        self.assertEqual(r.data["question_date"], years_ago(1).isoformat())


class PickerPagingTests(QuestionDateTests):
    """The test builder pages through questions, leaving out the ones the test already has."""

    def test_excluded_ids_are_left_out_of_the_count_and_the_pages(self):
        same_day = timezone.localdate()
        qs = [self.make(f"q{i}", same_day) for i in range(25)]
        used = ",".join(str(q.id) for q in qs[:20])
        r = self.client.get(f"/api/questions/?exclude_ids={used}&page_size=10&ordering=-question_date")
        self.assertEqual(r.data["count"], 5)
        self.assertEqual(sorted(x["id"] for x in r.data["results"]), sorted(q.id for q in qs[20:]))

    def test_questions_sharing_a_date_page_without_repeats_or_gaps(self):
        same_day = timezone.localdate()
        made = {self.make(f"q{i}", same_day).id for i in range(23)}
        seen = []
        for page in (1, 2, 3):
            seen += [x["id"] for x in self.client.get(f"/api/questions/?page={page}&page_size=10&ordering=-question_date").data["results"]]
        self.assertEqual(len(seen), 23)
        self.assertEqual(set(seen), made)

    def test_bad_exclude_ids_are_ignored(self):
        self.make("only", timezone.localdate())
        self.assertEqual(self.client.get("/api/questions/?exclude_ids=abc,,x1").data["count"], 1)

    def test_newest_pick_puts_undated_questions_last(self):
        undated = self.make("undated", None)
        dated = self.make("dated", years_ago(1))
        r = self.client.post("/api/questions/pick/", {"count": 5, "mode": "newest"}, format="json")
        self.assertEqual([q["id"] for q in r.data["questions"]], [dated.id, undated.id])


class UsageTests(APITestCase):
    """Each question says which tests it is already in, so an admin can see where it has been used."""

    def setUp(self):
        from exams.models import Exam, ExamQuestion, ExamSection
        self.Exam, self.ExamQuestion, self.ExamSection = Exam, ExamQuestion, ExamSection
        self.admin = User.objects.create_user(username="adm", email="a@x.com", password=PW, role=Role.ADMIN)
        self.subject = Subject.objects.create(name="CA")
        self.client.force_authenticate(self.admin)
        self.q = [self.make(f"q{i}") for i in range(3)]

    def make(self, text):
        q = Question.objects.create(text=text, subject=self.subject)
        QuestionOption.objects.create(question=q, text="a", is_correct=True, order=0)
        QuestionOption.objects.create(question=q, text="b", is_correct=False, order=1)
        return q

    def mock(self, title, questions, status="draft"):
        exam = self.Exam.objects.create(title=title, duration_minutes=10, status=status)
        sec = self.ExamSection.objects.create(exam=exam, name="S", order=0)
        for i, q in enumerate(questions):
            self.ExamQuestion.objects.create(section=sec, question=q, order=i, marks=1, negative_marks=0, question_version=1)
        return exam

    def used(self, q):
        return {u["title"] for u in self.client.get(f"/api/questions/{q.id}/").data["used_in"]}

    def test_a_question_lists_the_tests_it_is_in(self):
        self.mock("Mock 1", [self.q[0], self.q[1]])
        self.mock("Mock 2", [self.q[0]], status="published")
        self.assertEqual(self.used(self.q[0]), {"Mock 1", "Mock 2"})
        self.assertEqual(self.used(self.q[1]), {"Mock 1"})
        self.assertEqual(self.used(self.q[2]), set())

    def test_archived_tests_do_not_count(self):
        self.mock("Old mock", [self.q[0]], status="archived")
        self.assertEqual(self.used(self.q[0]), set())

    def test_filter_used_and_not_used(self):
        self.mock("Mock 1", [self.q[0]])
        ids = lambda qs: sorted(x["id"] for x in self.client.get(f"/api/questions/?{qs}").data["results"])  # noqa: E731
        self.assertEqual(ids("used=true"), [self.q[0].id])
        self.assertEqual(ids("used=false"), sorted([self.q[1].id, self.q[2].id]))
        self.assertEqual(len(ids("")), 3)

    def test_pick_can_skip_questions_already_used(self):
        self.mock("Mock 1", [self.q[0]])
        r = self.client.post("/api/questions/pick/", {"count": 10, "mode": "newest", "used": "false"}, format="json")
        self.assertEqual(sorted(x["id"] for x in r.data["questions"]), sorted([self.q[1].id, self.q[2].id]))

    def test_the_builder_shows_other_tests_a_question_is_also_in(self):
        a = self.mock("Mock 1", [self.q[0], self.q[1]])
        self.mock("Mock 2", [self.q[0]])
        r = self.client.get(f"/api/exams/{a.id}/")
        by_q = {x["question"]: x["also_in"] for x in r.data["sections"][0]["questions"]}
        self.assertEqual(by_q[self.q[0].id], ["Mock 2"])
        self.assertEqual(by_q[self.q[1].id], [])

    def test_listing_does_not_query_once_per_question(self):
        for i in range(12):
            self.mock(f"T{i}", [self.make(f"x{i}")])
        with self.assertNumQueries(8):  # count, page, options, tags, test usage (3), reports
            self.client.get("/api/questions/?page_size=20")
