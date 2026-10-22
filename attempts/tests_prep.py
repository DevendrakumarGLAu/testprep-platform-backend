from rest_framework.test import APITestCase

from accounts.models import Role, User
from exams.models import Exam, ExamCategory, ExamQuestion, ExamSection, Preparation
from questionbank.models import Question, QuestionOption, Subject

PW = "Str0ng!pass99"


def user(name, role=Role.STUDENT):
    return User.objects.create_user(username=name, email=f"{name}@x.com", password=PW, role=role)


class PrepBase(APITestCase):
    def setUp(self):
        self.admin = user("adm", Role.ADMIN)
        self.stu = user("stu")
        self.other = user("other")
        self.maths = Subject.objects.create(name="Maths")
        self.gk = Subject.objects.create(name="GK")
        self.reasoning = Subject.objects.create(name="Reasoning")
        self.english = Subject.objects.create(name="English")
        self.ssc = ExamCategory.objects.create(name="SSC", description="Staff Selection Commission")
        self.ssc.subjects.set([self.maths, self.gk, self.reasoning, self.english])
        self.bank = ExamCategory.objects.create(name="Banking")

    def make_test(self, title, category=None, kind="full_test", subject=None, year=None, **over):
        exam = Exam.objects.create(
            title=title, category=category, test_type=kind, subject=subject, year=year,
            duration_minutes=30, status=over.pop("status", "published"), max_attempts=0, **over,
        )
        sec = ExamSection.objects.create(exam=exam, name="S")
        q = Question.objects.create(text=f"{title} question", subject=subject or self.maths)
        QuestionOption.objects.create(question=q, text="a", is_correct=True)
        QuestionOption.objects.create(question=q, text="b", is_correct=False)
        ExamQuestion.objects.create(section=sec, question=q, order=0, marks=1, negative_marks=0, question_version=1)
        return exam

    def build_ssc(self):
        self.make_test("SSC Full Test 1", self.ssc)
        self.make_test("SSC Full Test 2", self.ssc)
        self.make_test("Maths only 1", self.ssc, "sectional", self.maths)
        self.make_test("Reasoning only 1", self.ssc, "sectional", self.reasoning)
        self.make_test("SSC CGL 2021", self.ssc, "previous_year", year=2021)
        self.make_test("SSC CGL 2023", self.ssc, "previous_year", year=2023)

    def login(self, u=None):
        self.client.force_authenticate(u or self.stu)


class AdminCatalogueTests(PrepBase):
    def setUp(self):
        super().setUp()
        self.client.force_authenticate(self.admin)

    def test_exam_with_sections(self):
        r = self.client.post("/api/exam-categories/", {"name": "Railways", "description": "RRB exams", "subjects": [self.maths.id, self.gk.id]}, format="json")
        self.assertEqual(r.status_code, 201, r.content)
        self.assertEqual(sorted(r.data["subject_names"]), ["GK", "Maths"])
        r = self.client.patch(f"/api/exam-categories/{r.data['id']}/", {"subjects": [self.gk.id]}, format="json")
        self.assertEqual(r.data["subject_names"], ["GK"])

    def test_exam_with_tests_cannot_be_deleted_but_empty_one_can(self):
        self.make_test("T", self.ssc)
        self.assertEqual(self.client.delete(f"/api/exam-categories/{self.ssc.id}/").status_code, 409)
        self.assertEqual(self.client.delete(f"/api/exam-categories/{self.bank.id}/").status_code, 204)
        self.assertTrue(Exam.objects.filter(category=self.ssc).exists())

    def post_exam(self, **body):
        base = {"title": "T", "category": self.ssc.id, "duration_minutes": 30}
        return self.client.post("/api/exams/", {**base, **body}, format="json")

    def test_test_type_rules(self):
        self.assertEqual(self.post_exam(test_type="sectional").status_code, 400)  # needs a subject
        r = self.post_exam(test_type="sectional", subject=self.maths.id)
        self.assertEqual((r.status_code, r.data["subject_name"]), (201, "Maths"))
        self.assertEqual(self.post_exam(test_type="previous_year").status_code, 400)  # needs a year
        self.assertEqual(self.post_exam(test_type="previous_year", year=1800).status_code, 400)
        r = self.post_exam(test_type="previous_year", year=2022)
        self.assertEqual((r.status_code, r.data["year"]), (201, 2022))

    def test_full_test_ignores_subject_and_year(self):
        r = self.post_exam(test_type="full_test", subject=self.maths.id, year=2020)
        self.assertEqual(r.status_code, 201)
        self.assertEqual((r.data["subject"], r.data["year"]), (None, None))

    def test_changing_type_clears_stale_values_and_filters_work(self):
        r = self.post_exam(test_type="sectional", subject=self.maths.id)
        eid = r.data["id"]
        r = self.client.patch(f"/api/exams/{eid}/", {"test_type": "full_test"}, format="json")
        self.assertEqual((r.data["test_type"], r.data["subject"]), ("full_test", None))
        self.post_exam(test_type="previous_year", year=2020)
        self.assertEqual(self.client.get("/api/exams/?test_type=previous_year").data["count"], 1)
        self.assertEqual(self.client.get(f"/api/exams/?category={self.ssc.id}").data["count"], 2)

    def test_type_can_be_edited_after_publish(self):
        e = self.make_test("Live", self.ssc, "sectional", self.maths)
        r = self.client.patch(f"/api/exams/{e.id}/", {"subject": self.gk.id}, format="json")
        self.assertEqual(r.status_code, 200, r.content)  # it is a label, not a scoring rule


class ChooseExamTests(PrepBase):
    def test_lists_only_exams_with_tests_the_student_can_see(self):
        self.build_ssc()
        self.login()
        d = self.client.get("/api/student/prep/").data
        self.assertEqual([e["name"] for e in d["exams"]], ["SSC"])  # Banking has no tests yet
        ssc = d["exams"][0]
        self.assertEqual(ssc["subjects"], ["English", "GK", "Maths", "Reasoning"])
        self.assertEqual(ssc["counts"], {"full_test": 2, "sectional": 2, "previous_year": 2, "total": 6})
        self.assertFalse(ssc["preparing"])
        self.assertEqual(d["preparing"], [])

    def test_hidden_drafts_and_unassigned_tests_do_not_count(self):
        self.make_test("Draft", self.bank, status="draft")
        self.make_test("Private", self.bank, access_type="assigned")
        self.login()
        self.assertEqual(self.client.get("/api/student/prep/").data["exams"], [])
        Exam.objects.get(title="Private").assigned_students.add(self.stu)
        self.assertEqual([e["name"] for e in self.client.get("/api/student/prep/").data["exams"]], ["Banking"])

    def test_choose_and_change_exams(self):
        self.build_ssc()
        self.make_test("Bank test", self.bank)
        self.login()
        r = self.client.put("/api/student/prep/", {"category_ids": [self.ssc.public_id]}, format="json")
        self.assertEqual(r.status_code, 200, r.content)
        self.assertEqual(r.data["preparing"], [self.ssc.public_id])
        self.assertTrue(next(e for e in r.data["exams"] if e["id"] == self.ssc.public_id)["preparing"])
        r = self.client.put("/api/student/prep/", {"category_ids": [self.ssc.public_id, self.bank.public_id]}, format="json")
        self.assertEqual(sorted(r.data["preparing"]), sorted([self.ssc.public_id, self.bank.public_id]))
        r = self.client.put("/api/student/prep/", {"category_ids": [self.bank.public_id]}, format="json")
        self.assertEqual(r.data["preparing"], [self.bank.public_id])
        r = self.client.put("/api/student/prep/", {"category_ids": []}, format="json")
        self.assertEqual(r.data["preparing"], [])
        # Another student is unaffected.
        self.client.put("/api/student/prep/", {"category_ids": [self.ssc.public_id]}, format="json")
        self.login(self.other)
        self.assertEqual(self.client.get("/api/student/prep/").data["preparing"], [])

    def test_rejects_bad_selections(self):
        self.build_ssc()
        self.login()
        put = lambda body: self.client.put("/api/student/prep/", body, format="json").status_code  # noqa: E731
        self.assertEqual(put({}), 400)
        self.assertEqual(put({"category_ids": "ssc"}), 400)
        self.assertEqual(put({"category_ids": [99999]}), 400)
        self.ssc.is_active = False
        self.ssc.save()
        self.assertEqual(put({"category_ids": [self.ssc.public_id]}), 400)
        self.assertEqual(Preparation.objects.count(), 0)


class HubTests(PrepBase):
    def test_groups_full_subject_and_previous_year_tests(self):
        self.build_ssc()
        self.make_test("Other exam test", self.bank)
        self.login()
        d = self.client.get(f"/api/student/prep/{self.ssc.public_id}/").data
        self.assertEqual(d["exam"]["name"], "SSC")
        self.assertEqual([t["title"] for t in d["full_tests"]], ["SSC Full Test 1", "SSC Full Test 2"])
        self.assertEqual([t["year"] for t in d["previous_year"]], [2023, 2021])  # newest first
        groups = {g["subject_name"]: [t["title"] for t in g["tests"]] for g in d["subject_tests"]}
        self.assertEqual(groups["Maths"], ["Maths only 1"])
        self.assertEqual(groups["Reasoning"], ["Reasoning only 1"])
        # Every section of the exam is listed, even those without tests yet.
        self.assertEqual(groups["GK"], [])
        self.assertEqual(groups["English"], [])
        self.assertEqual(set(groups), {"Maths", "GK", "Reasoning", "English"})
        self.assertEqual(d["progress"], {"total": 6, "attempted": 0})
        self.assertNotIn("Other exam test", str(d))

    def test_subjects_without_tests_are_listed_last(self):
        self.make_test("Reasoning only 1", self.ssc, "sectional", self.reasoning)
        self.make_test("English only 1", self.ssc, "sectional", self.english)
        self.login()
        names = [g["subject_name"] for g in self.client.get(f"/api/student/prep/{self.ssc.public_id}/").data["subject_tests"]]
        self.assertEqual(names[:2], ["English", "Reasoning"])  # the ones with tests, in the exam's order
        self.assertEqual(set(names[2:]), {"Maths", "GK"})  # upcoming: no tests yet

    def test_cards_carry_type_information(self):
        self.build_ssc()
        self.login()
        d = self.client.get(f"/api/student/prep/{self.ssc.public_id}/").data
        maths = next(g for g in d["subject_tests"] if g["subject_name"] == "Maths")["tests"][0]
        self.assertEqual((maths["test_type"], maths["test_type_label"], maths["subject_name"]), ("sectional", "Subject test", "Maths"))
        py = d["previous_year"][0]
        self.assertEqual((py["test_type_label"], py["year"]), ("Previous year paper", 2023))

    def test_student_can_attempt_whichever_test_they_choose(self):
        self.build_ssc()
        self.login()
        d = self.client.get(f"/api/student/prep/{self.ssc.public_id}/").data
        picks = [d["full_tests"][1], d["previous_year"][0], next(g for g in d["subject_tests"] if g["tests"])["tests"][0]]
        for card in picks:
            r = self.client.post(f"/api/student/exams/{card['id']}/start/")
            self.assertEqual(r.status_code, 201, card["title"])
        d = self.client.get(f"/api/student/prep/{self.ssc.public_id}/").data
        self.assertEqual(d["progress"], {"total": 6, "attempted": 3})

    def test_hub_hides_tests_the_student_cannot_see(self):
        self.make_test("Visible", self.ssc)
        self.make_test("Draft", self.ssc, status="draft")
        self.make_test("Not mine", self.ssc, access_type="assigned")
        self.login()
        titles = [t["title"] for t in self.client.get(f"/api/student/prep/{self.ssc.public_id}/").data["full_tests"]]
        self.assertEqual(titles, ["Visible"])

    def test_unknown_or_inactive_exam_is_404(self):
        self.login()
        self.assertEqual(self.client.get("/api/student/prep/9999/").status_code, 404)
        self.ssc.is_active = False
        self.ssc.save()
        self.assertEqual(self.client.get(f"/api/student/prep/{self.ssc.public_id}/").status_code, 404)

    def test_dashboard_shows_progress_for_chosen_exams(self):
        self.build_ssc()
        self.login()
        self.assertEqual(self.client.get("/api/student/dashboard/").data["preparing"], [])
        self.client.put("/api/student/prep/", {"category_ids": [self.ssc.public_id]}, format="json")
        card = self.client.get(f"/api/student/prep/{self.ssc.public_id}/").data["full_tests"][0]
        aid = self.client.post(f"/api/student/exams/{card['id']}/start/").data["attempt_id"]
        self.client.post(f"/api/student/attempts/{aid}/submit/")
        p = self.client.get("/api/student/dashboard/").data["preparing"]
        self.assertEqual(p, [{"id": self.ssc.public_id, "name": "SSC", "total_tests": 6, "attempted_tests": 1}])

    def test_existing_list_can_filter_by_type_and_category(self):
        self.build_ssc()
        self.make_test("Loose test")  # no exam
        self.login()
        self.assertEqual(len(self.client.get("/api/student/exams/?test_type=previous_year").data), 2)
        self.assertEqual(len(self.client.get(f"/api/student/exams/?category={self.ssc.public_id}").data), 6)
        self.assertEqual(len(self.client.get("/api/student/exams/").data), 7)
