from notifications.services import students_for_exam

from accounts.models import Role
from exams.models import ExamCategory, ExamGroup, Preparation

from .tests_prep import PrepBase, user


class FamilyBase(PrepBase):
    """SSC as a family with CHSL, CGL and MTS under it."""

    def setUp(self):
        super().setUp()
        self.family = ExamGroup.objects.create(name="SSC Exams", order=1)
        self.rail = ExamGroup.objects.create(name="Railways", order=2)
        self.mts = ExamCategory.objects.create(name="SSC MTS", group=self.family)
        self.chsl = ExamCategory.objects.create(name="SSC CHSL", group=self.family)
        self.cgl = ExamCategory.objects.create(name="SSC CGL", group=self.family)
        self.ntpc = ExamCategory.objects.create(name="RRB NTPC", group=self.rail)


class ExamFamilyAdminTests(FamilyBase):
    def setUp(self):
        super().setUp()
        self.client.force_authenticate(self.admin)

    def test_students_cannot_manage_families(self):
        self.client.force_authenticate(self.stu)
        self.assertEqual(self.client.get("/api/exam-groups/").status_code, 403)
        self.assertEqual(self.client.post("/api/exam-groups/", {"name": "X"}, format="json").status_code, 403)

    def test_create_rename_and_order_a_family(self):
        r = self.client.post("/api/exam-groups/", {"name": "Banking", "description": "IBPS and SBI", "order": 3}, format="json")
        self.assertEqual(r.status_code, 201, r.content)
        r = self.client.patch(f"/api/exam-groups/{r.data['id']}/", {"name": "Bank exams"}, format="json")
        self.assertEqual(r.data["name"], "Bank exams")
        names = [g["name"] for g in self.client.get("/api/exam-groups/").data["results"]]
        self.assertEqual(names, ["SSC Exams", "Railways", "Bank exams"])

    def test_family_names_are_unique_ignoring_case(self):
        r = self.client.post("/api/exam-groups/", {"name": "ssc exams"}, format="json")
        self.assertEqual(r.status_code, 400)

    def test_family_lists_how_many_exams_it_holds(self):
        rows = {g["name"]: g["exam_count"] for g in self.client.get("/api/exam-groups/").data["results"]}
        self.assertEqual(rows, {"SSC Exams": 3, "Railways": 1})

    def test_family_with_exams_cannot_be_deleted_but_an_empty_one_can(self):
        self.assertEqual(self.client.delete(f"/api/exam-groups/{self.family.id}/").status_code, 409)
        empty = ExamGroup.objects.create(name="Empty")
        self.assertEqual(self.client.delete(f"/api/exam-groups/{empty.id}/").status_code, 204)

    def test_an_exam_is_placed_in_a_family_and_moved_out_again(self):
        r = self.client.post("/api/exam-categories/", {"name": "SSC GD", "group": self.family.id}, format="json")
        self.assertEqual(r.status_code, 201, r.content)
        self.assertEqual(r.data["group_name"], "SSC Exams")
        r = self.client.patch(f"/api/exam-categories/{r.data['id']}/", {"group": None}, format="json")
        self.assertIsNone(r.data["group"])


class ExamPatternTests(FamilyBase):
    def setUp(self):
        super().setUp()
        self.client.force_authenticate(self.admin)

    def save(self, pattern):
        return self.client.patch(f"/api/exam-categories/{self.cgl.id}/", {"pattern": pattern}, format="json")

    def test_a_pattern_is_saved_and_returned(self):
        pattern = {
            "duration_minutes": 60, "marks_per_question": 2, "passing_marks": 0, "negative_option": "fixed", "negative_value": 0.5,
            "sectional_timing": False, "max_attempts": 0,
            "sections": [{"name": "General Intelligence", "duration_minutes": None}, {"name": "English", "duration_minutes": 15}],
        }
        r = self.save(pattern)
        self.assertEqual(r.status_code, 200, r.content)
        self.assertEqual(r.data["pattern"]["duration_minutes"], 60)
        self.assertEqual([s["name"] for s in r.data["pattern"]["sections"]], ["General Intelligence", "English"])
        self.cgl.refresh_from_db()
        self.assertEqual(self.cgl.pattern["negative_option"], "fixed")

    def test_unknown_settings_are_dropped_and_blank_ones_ignored(self):
        r = self.save({"duration_minutes": "45", "colour": "red", "marks_per_question": ""})
        self.assertEqual(r.data["pattern"], {"duration_minutes": 45})

    def test_an_empty_pattern_clears_it(self):
        self.save({"duration_minutes": 30})
        self.assertEqual(self.save({}).data["pattern"], {})

    def test_bad_values_are_refused(self):
        for bad in (
            {"duration_minutes": 0}, {"duration_minutes": 700}, {"duration_minutes": 12.5}, {"marks_per_question": 0},
            {"negative_option": "sometimes"}, {"sections": [{"name": ""}]}, {"sections": [{"name": "A", "duration_minutes": 0}]},
            {"sections": "Maths"}, "not a pattern", {"max_attempts": 99},
        ):
            self.assertEqual(self.save(bad).status_code, 400, bad)


class SharedTestAdminTests(FamilyBase):
    def setUp(self):
        super().setUp()
        self.client.force_authenticate(self.admin)

    def post_exam(self, **body):
        base = {"title": "Maths practice", "category": self.cgl.id, "duration_minutes": 30}
        base.update(body)
        return self.client.post("/api/exams/", base, format="json")

    def test_a_test_can_also_be_shown_in_other_exams(self):
        r = self.post_exam(also_in=[self.chsl.id, self.mts.id])
        self.assertEqual(r.status_code, 201, r.content)
        self.assertEqual(sorted(r.data["also_in"]), sorted([self.chsl.id, self.mts.id]))

    def test_its_own_exam_is_never_listed_as_a_shared_one(self):
        r = self.post_exam(also_in=[self.cgl.id, self.chsl.id])
        self.assertEqual(r.data["also_in"], [self.chsl.id])

    def test_the_list_can_be_changed_later_and_emptied(self):
        eid = self.post_exam(also_in=[self.chsl.id]).data["id"]
        r = self.client.patch(f"/api/exams/{eid}/", {"also_in": [self.mts.id]}, format="json")
        self.assertEqual(r.data["also_in"], [self.mts.id])
        r = self.client.patch(f"/api/exams/{eid}/", {"also_in": []}, format="json")
        self.assertEqual(r.data["also_in"], [])

    def test_an_exam_that_shares_tests_cannot_be_deleted(self):
        self.post_exam(also_in=[self.chsl.id])
        self.assertEqual(self.client.delete(f"/api/exam-categories/{self.chsl.id}/").status_code, 409)
        self.assertEqual(self.client.delete(f"/api/exam-categories/{self.mts.id}/").status_code, 204)

    def test_exam_count_includes_shared_tests(self):
        self.post_exam(also_in=[self.chsl.id])
        rows = {c["name"]: c["exam_count"] for c in self.client.get("/api/exam-categories/").data["results"]}
        self.assertEqual((rows["SSC CGL"], rows["SSC CHSL"], rows["SSC MTS"]), (1, 1, 0))

    def test_duplicating_a_test_keeps_where_it_is_shown(self):
        eid = self.post_exam(also_in=[self.chsl.id]).data["id"]
        r = self.client.post(f"/api/exams/{eid}/duplicate/")
        self.assertEqual(r.status_code, 201, r.content)
        copy = self.client.get(f"/api/exams/{r.data['id']}/").data
        self.assertEqual(copy["also_in"], [self.chsl.id])


class StudentFamilyTests(FamilyBase):
    def setUp(self):
        super().setUp()
        self.login()

    def prep(self):
        return self.client.get("/api/student/prep/").data

    def test_exams_come_with_their_family_and_families_in_order(self):
        for cat in (self.cgl, self.mts, self.chsl, self.ntpc):
            self.make_test(f"{cat.name} full", cat)
        d = self.prep()
        by_name = {e["name"]: e for e in d["exams"]}
        self.assertEqual(by_name["SSC CGL"]["group"]["name"], "SSC Exams")
        self.assertEqual(by_name["RRB NTPC"]["group"]["name"], "Railways")
        self.assertEqual([f["name"] for f in d["families"]], ["SSC Exams", "Railways"])
        self.assertEqual([e["name"] for e in d["exams"]][:3], ["SSC CGL", "SSC CHSL", "SSC MTS"])

    def test_an_exam_outside_any_family_has_no_group(self):
        self.make_test("Banking test", self.bank)
        e = next(e for e in self.prep()["exams"] if e["name"] == "Banking")
        self.assertIsNone(e["group"])

    def test_a_hidden_family_is_not_shown(self):
        self.make_test("Rail full", self.ntpc)
        ExamGroup.objects.filter(pk=self.rail.pk).update(is_active=False)
        d = self.prep()
        self.assertEqual([f["name"] for f in d["families"]], [])
        self.assertIsNone(next(e for e in d["exams"] if e["name"] == "RRB NTPC")["group"])

    def test_a_shared_test_appears_in_every_exam_it_is_shared_with(self):
        t = self.make_test("Maths for all", self.cgl, "sectional", self.maths)
        t.also_in.set([self.chsl, self.mts])
        for cat in (self.cgl, self.chsl, self.mts):
            hub = self.client.get(f"/api/student/prep/{cat.public_id}/").data
            titles = [c["title"] for g in hub["subject_tests"] for c in g["tests"]]
            self.assertIn("Maths for all", titles, cat.name)
        hub = self.client.get(f"/api/student/prep/{self.ntpc.public_id}/")
        self.assertNotIn("Maths for all", str(hub.data))

    def test_shared_tests_are_counted_in_each_exam_and_by_type(self):
        full = self.make_test("Common full", self.cgl)
        full.also_in.set([self.chsl])
        old = self.make_test("Common 2022", self.cgl, "previous_year", year=2022)
        old.also_in.set([self.chsl])
        counts = {e["name"]: e["counts"] for e in self.prep()["exams"]}
        self.assertEqual((counts["SSC CGL"]["full_test"], counts["SSC CGL"]["previous_year"], counts["SSC CGL"]["total"]), (1, 1, 2))
        self.assertEqual((counts["SSC CHSL"]["full_test"], counts["SSC CHSL"]["previous_year"], counts["SSC CHSL"]["total"]), (1, 1, 2))

    def test_an_exam_with_only_shared_tests_is_still_offered(self):
        t = self.make_test("Only elsewhere", self.cgl)
        t.also_in.set([self.mts])
        names = [e["name"] for e in self.prep()["exams"]]
        self.assertIn("SSC MTS", names)

    def test_the_test_list_filter_includes_shared_tests_once(self):
        t = self.make_test("Shared list test", self.cgl)
        t.also_in.set([self.chsl, self.mts])
        r = self.client.get("/api/student/exams/", {"category": self.chsl.public_id})
        self.assertEqual([c["title"] for c in r.data], ["Shared list test"])
        r = self.client.get("/api/student/exams/", {"category": self.cgl.public_id})
        self.assertEqual([c["title"] for c in r.data], ["Shared list test"])

    def test_dashboard_progress_counts_shared_tests_for_each_exam(self):
        t = self.make_test("Progress test", self.cgl)
        t.also_in.set([self.chsl])
        Preparation.objects.create(student=self.stu, category=self.chsl)
        d = self.client.get("/api/student/dashboard/").data
        row = next(p for p in d["preparing"] if p["name"] == "SSC CHSL")
        self.assertEqual((row["total_tests"], row["attempted_tests"]), (1, 0))
        aid = self.client.post(f"/api/student/exams/{t.public_id}/start/").data["attempt_id"]
        self.client.post(f"/api/student/attempts/{aid}/submit/")
        row = next(p for p in self.client.get("/api/student/dashboard/").data["preparing"] if p["name"] == "SSC CHSL")
        self.assertEqual((row["total_tests"], row["attempted_tests"]), (1, 1))

    def test_hub_names_the_family(self):
        self.make_test("T", self.cgl)
        exam = self.client.get(f"/api/student/prep/{self.cgl.public_id}/").data["exam"]
        self.assertEqual(exam["group"]["name"], "SSC Exams")


class SharedTestAudienceTests(FamilyBase):
    def test_students_preparing_for_a_sharing_exam_are_told_about_the_test(self):
        t = self.make_test("Shared new test", self.cgl)
        t.also_in.set([self.chsl])
        a, b, c = user("pa"), user("pb"), user("pc")
        Preparation.objects.create(student=a, category=self.cgl)
        Preparation.objects.create(student=b, category=self.chsl)
        Preparation.objects.create(student=c, category=self.mts)
        self.assertEqual(set(students_for_exam(t)), {a, b})
        self.assertEqual(Role.STUDENT, a.role)

    def test_public_page_of_a_sharing_exam_lists_the_test(self):
        t = self.make_test("Public shared", self.cgl, access_type="public")
        t.also_in.set([self.chsl])
        r = self.client.get(f"/api/public/exams/{self.chsl.public_id}/")
        self.assertEqual(r.status_code, 200)
        self.assertEqual([x["title"] for x in r.data["tests"]], ["Public shared"])


class TestsPageDataTests(FamilyBase):
    """What the student Tests page needs to group tests under their exams."""

    def setUp(self):
        super().setUp()
        self.login()

    def queries(self, path="/api/student/exams/"):
        from django.db import connection
        from django.test.utils import CaptureQueriesContext

        with CaptureQueriesContext(connection) as q:
            self.client.get(path)
        return len(q)

    def test_cards_say_which_other_exams_show_the_test(self):
        t = self.make_test("Shared maths", self.cgl, "sectional", self.maths)
        t.also_in.set([self.chsl, self.mts])
        self.make_test("Solo", self.cgl)
        cards = {c["title"]: c for c in self.client.get("/api/student/exams/").data}
        self.assertEqual(sorted(cards["Shared maths"]["also_in"]), sorted([self.chsl.public_id, self.mts.public_id]))
        self.assertEqual(cards["Solo"]["also_in"], [])
        self.assertEqual(cards["Solo"]["category"], self.cgl.public_id)

    def test_the_list_does_not_query_once_per_card(self):
        for i in range(2):
            self.make_test(f"T{i}", self.cgl).also_in.set([self.chsl])
        few = self.queries()
        for i in range(2, 10):
            self.make_test(f"T{i}", self.cgl).also_in.set([self.chsl, self.mts])
        many = self.queries()
        self.assertLessEqual(many, few + 2)

    def test_filter_choices_are_ordered_by_family_and_carry_the_family(self):
        for cat in (self.cgl, self.mts, self.ntpc, self.bank):
            self.make_test(f"{cat.name} test", cat)
        cats = self.client.get("/api/student/filters/").data["categories"]
        by_name = {c["name"]: c for c in cats}
        self.assertEqual(by_name["SSC CGL"]["group"]["name"], "SSC Exams")
        self.assertEqual(by_name["RRB NTPC"]["group"]["name"], "Railways")
        self.assertIsNone(by_name["Banking"]["group"])
        order = [c["name"] for c in cats]
        self.assertLess(order.index("SSC CGL"), order.index("SSC MTS"))
        self.assertLess(order.index("SSC MTS"), order.index("RRB NTPC"))

    def test_an_exam_that_only_shows_a_shared_test_is_a_filter_choice(self):
        t = self.make_test("Only elsewhere", self.cgl)
        t.also_in.set([self.mts])
        names = [c["name"] for c in self.client.get("/api/student/filters/").data["categories"]]
        self.assertIn("SSC MTS", names)
        self.assertNotIn("SSC CHSL", names)  # nothing in it


class MiniMockTests(FamilyBase):
    """Mini mocks are their own kind of test, with their own tab in an exam."""

    def test_admin_can_make_a_mini_mock_and_unknown_types_are_refused(self):
        self.client.force_authenticate(self.admin)
        r = self.client.post("/api/exams/", {"title": "Mini 1", "category": self.cgl.id, "duration_minutes": 15, "test_type": "mini_mock"}, format="json")
        self.assertEqual(r.status_code, 201, r.content)
        self.assertEqual(r.data["test_type"], "mini_mock")
        r = self.client.post("/api/exams/", {"title": "X", "category": self.cgl.id, "duration_minutes": 15, "test_type": "quiz"}, format="json")
        self.assertEqual(r.status_code, 400)

    def test_hub_lists_each_kind_in_its_own_group(self):
        self.login()
        self.make_test("Full A", self.cgl)
        self.make_test("Mini A", self.cgl, "mini_mock")
        self.make_test("Mini B", self.cgl, "mini_mock")
        self.make_test("Old 2022", self.cgl, "previous_year", year=2022)
        self.make_test("Maths only", self.cgl, "sectional", self.maths)
        hub = self.client.get(f"/api/student/prep/{self.cgl.public_id}/").data
        self.assertEqual([c["title"] for c in hub["full_tests"]], ["Full A"])
        self.assertEqual(sorted(c["title"] for c in hub["mini_mocks"]), ["Mini A", "Mini B"])
        self.assertEqual([c["title"] for c in hub["previous_year"]], ["Old 2022"])
        self.assertEqual([c["title"] for g in hub["subject_tests"] for c in g["tests"]], ["Maths only"])
        self.assertEqual(hub["progress"]["total"], 5)

    def test_a_mini_mock_is_not_mixed_into_full_tests_or_subject_tests(self):
        self.login()
        self.make_test("Mini only", self.cgl, "mini_mock")
        hub = self.client.get(f"/api/student/prep/{self.cgl.public_id}/").data
        self.assertEqual(hub["full_tests"], [])
        self.assertEqual([t for g in hub["subject_tests"] for t in g["tests"]], [])

    def test_counts_include_mini_mocks_and_a_shared_one_counts_in_each_exam(self):
        self.login()
        t = self.make_test("Shared mini", self.cgl, "mini_mock")
        t.also_in.set([self.chsl])
        counts = {e["name"]: e["counts"] for e in self.client.get("/api/student/prep/").data["exams"]}
        self.assertEqual((counts["SSC CGL"]["mini_mock"], counts["SSC CGL"]["total"]), (1, 1))
        self.assertEqual((counts["SSC CHSL"]["mini_mock"], counts["SSC CHSL"]["total"]), (1, 1))
        self.assertEqual(counts["SSC MTS"]["mini_mock"] if "SSC MTS" in counts else 0, 0)

    def test_the_test_card_names_the_type(self):
        self.login()
        self.make_test("Mini card", self.cgl, "mini_mock")
        card = self.client.get("/api/student/exams/").data[0]
        self.assertEqual((card["test_type"], card["test_type_label"]), ("mini_mock", "Mini mock"))


class DashboardFamilyTests(FamilyBase):
    def test_each_exam_the_student_prepares_for_names_its_family(self):
        self.login()
        self.make_test("CGL full", self.cgl)
        self.make_test("Bank full", self.bank)
        Preparation.objects.create(student=self.stu, category=self.cgl)
        Preparation.objects.create(student=self.stu, category=self.bank)
        rows = {p["name"]: p for p in self.client.get("/api/student/dashboard/").data["preparing"]}
        self.assertEqual(rows["SSC CGL"]["group"], {"id": self.family.public_id, "name": "SSC Exams"})
        self.assertIsNone(rows["Banking"]["group"])

    def test_a_hidden_family_is_not_named(self):
        self.login()
        self.make_test("CGL full", self.cgl)
        Preparation.objects.create(student=self.stu, category=self.cgl)
        ExamGroup.objects.filter(pk=self.family.pk).update(is_active=False)
        row = self.client.get("/api/student/dashboard/").data["preparing"][0]
        self.assertIsNone(row["group"])


class TestOrderTests(FamilyBase):
    """A series reads Mock 1, 2, 3 ... 10 whatever order the tests were created in (and on any database)."""

    TITLES = ["Mock 10", "Mock 2", "mock 1", "Mock 11", "Mock 3"]
    WANTED = ["mock 1", "Mock 2", "Mock 3", "Mock 10", "Mock 11"]

    def setUp(self):
        super().setUp()
        for t in self.TITLES:
            self.make_test(t, self.cgl)
        self.client.force_authenticate(self.stu)

    def test_the_tests_of_an_exam_are_in_natural_order(self):
        d = self.client.get(f"/api/student/prep/{self.cgl.public_id}/").data
        self.assertEqual([c["title"] for c in d["full_tests"]], self.WANTED)

    def test_the_all_tests_list_is_in_natural_order_too(self):
        r = self.client.get("/api/student/exams/", {"category": self.cgl.public_id})
        self.assertEqual([c["title"] for c in r.data], self.WANTED)

    def test_scheduled_tests_come_first_soonest_first_then_the_rest(self):
        from datetime import timedelta

        from django.utils import timezone
        now = timezone.now()
        self.make_test("Zeta later", self.cgl, start_at=now + timedelta(days=2))
        self.make_test("Alpha sooner", self.cgl, start_at=now + timedelta(days=1))
        r = self.client.get("/api/student/exams/", {"category": self.cgl.public_id})
        self.assertEqual([c["title"] for c in r.data], ["Alpha sooner", "Zeta later"] + self.WANTED)
