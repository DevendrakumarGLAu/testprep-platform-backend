from django.test import SimpleTestCase

from attempts.tests_families import FamilyBase

from exams.models import Exam

from .tabs import custom_keys, default_tabs, is_custom, resolve_tabs


class ResolveTabsTests(SimpleTestCase):
    def keys(self, tabs):
        return [t["key"] for t in tabs]

    def test_nothing_saved_gives_the_four_standard_tabs(self):
        t = resolve_tabs([])
        self.assertEqual(self.keys(t), ["full", "mini", "previous", "subject"])
        self.assertEqual([x["label"] for x in t], ["Full tests", "Mini mocks", "Previous year", "Subject wise"])
        self.assertTrue(all(x["visible"] for x in t))
        self.assertEqual(resolve_tabs(None), default_tabs())

    def test_order_names_and_hiding_are_kept(self):
        t = resolve_tabs([{"key": "subject", "label": " By subject ", "visible": True}, {"key": "mini", "visible": False}])
        self.assertEqual(self.keys(t), ["subject", "mini", "full", "previous"])  # the ones not mentioned follow, shown
        self.assertEqual(t[0]["label"], "By subject")
        self.assertEqual(t[1]["label"], "Mini mocks")  # no name given: the standard one
        self.assertFalse(t[1]["visible"])
        self.assertTrue(t[2]["visible"])

    def test_damaged_entries_are_ignored_and_something_always_shows(self):
        t = resolve_tabs(["x", {"key": "nope"}, {"key": "full"}, {"key": "full", "label": "Again"}])
        self.assertEqual(self.keys(t), ["full", "mini", "previous", "subject"])
        self.assertEqual(t[0]["label"], "Full tests")
        hidden = resolve_tabs([{"key": k, "visible": False} for k in ("full", "mini", "previous", "subject")])
        self.assertTrue(hidden[0]["visible"])


class CategoryTabsApiTests(FamilyBase):
    def patch(self, tabs):
        return self.client.patch(f"/api/exam-categories/{self.cgl.id}/", {"tabs": tabs}, format="json")

    def hub(self):
        return self.client.get(f"/api/student/prep/{self.cgl.public_id}/")

    def test_the_admin_can_hide_rename_and_reorder_and_students_get_it(self):
        self.client.force_authenticate(self.admin)
        r = self.patch([{"key": "subject", "label": "By subject", "visible": True}, {"key": "previous", "label": "PYQ", "visible": False}])
        self.assertEqual(r.status_code, 200, r.content)
        self.assertEqual([t["key"] for t in r.data["tabs"]], ["subject", "previous", "full", "mini"])
        self.client.force_authenticate(self.stu)
        tabs = self.hub().data["tabs"]
        self.assertEqual([(t["key"], t["label"], t["visible"]) for t in tabs][:2], [("subject", "By subject", True), ("previous", "PYQ", False)])

    def test_an_exam_with_nothing_saved_shows_the_standard_tabs(self):
        self.client.force_authenticate(self.stu)
        self.assertEqual([t["key"] for t in self.hub().data["tabs"]], ["full", "mini", "previous", "subject"])
        self.client.force_authenticate(self.admin)
        self.assertEqual(len(self.client.get(f"/api/exam-categories/{self.mts.id}/").data["tabs"]), 4)

    def test_bad_tabs_are_refused(self):
        self.client.force_authenticate(self.admin)
        self.assertEqual(self.patch([{"key": "nope"}]).status_code, 400)
        self.assertEqual(self.patch([{"key": "full"}, {"key": "full"}]).status_code, 400)
        self.assertEqual(self.patch([{"key": "full", "label": "x" * 31}]).status_code, 400)
        self.assertEqual(self.patch("full").status_code, 400)
        self.assertEqual(self.patch([{"key": k, "visible": False} for k in ("full", "mini", "previous", "subject")]).status_code, 400)

    def test_students_cannot_change_tabs(self):
        self.client.force_authenticate(self.stu)
        self.assertEqual(self.patch([{"key": "full"}]).status_code, 403)

    def test_an_empty_list_puts_the_standard_tabs_back(self):
        self.client.force_authenticate(self.admin)
        self.patch([{"key": "mini", "visible": False}])
        self.assertEqual(self.patch([]).status_code, 200)
        self.cgl.refresh_from_db()
        self.assertEqual(self.cgl.tabs, [])


CA = {"key": "cabc12", "label": "Current affairs", "visible": True}


class CustomTabTests(SimpleTestCase):
    def test_a_tab_of_the_admins_own_is_kept_with_its_name_and_marked_custom(self):
        t = resolve_tabs([CA, {"key": "full"}])
        self.assertEqual([x["key"] for x in t], ["cabc12", "full", "mini", "previous", "subject"])
        self.assertTrue(t[0]["custom"])
        self.assertFalse(t[1]["custom"])
        self.assertEqual(custom_keys([CA]), {"cabc12"})

    def test_what_counts_as_a_custom_key(self):
        self.assertTrue(is_custom("c1k9x2"))
        for bad in ("full", "C1", "c", "x1", "c" + "a" * 16, "c-1", "", None, 5):
            self.assertFalse(is_custom(bad), bad)

    def test_a_custom_tab_without_a_name_is_dropped(self):
        self.assertEqual(custom_keys([{"key": "cabc12", "label": "  "}]), set())


class CustomTabApiTests(FamilyBase):
    def setUp(self):
        super().setUp()
        self.client.force_authenticate(self.admin)
        r = self.client.patch(f"/api/exam-categories/{self.cgl.id}/", {"tabs": [CA, {"key": "full", "label": "Full tests"}]}, format="json")
        self.assertEqual(r.status_code, 200, r.content)

    def hub(self, cat=None):
        return self.client.get(f"/api/student/prep/{(cat or self.cgl).public_id}/")

    def test_the_admin_can_add_a_tab_and_it_comes_back_first(self):
        r = self.client.get(f"/api/exam-categories/{self.cgl.id}/")
        self.assertEqual([(t["key"], t["custom"]) for t in r.data["tabs"]][:2], [("cabc12", True), ("full", False)])

    def test_bad_new_tabs_are_refused(self):
        patch = lambda tabs: self.client.patch(f"/api/exam-categories/{self.cgl.id}/", {"tabs": tabs}, format="json").status_code  # noqa: E731
        self.assertEqual(patch([{"key": "cabc12", "label": ""}]), 400)
        self.assertEqual(patch([{"key": "bad key", "label": "x"}]), 400)
        self.assertEqual(patch([CA, CA]), 400)
        self.assertEqual(patch([{"key": f"c{i}", "label": f"T{i}"} for i in range(9)]), 400)  # 9 + 4 built-in is over 12

    def test_a_test_in_a_custom_tab_shows_there_and_not_in_its_type_tab(self):
        e = self.make_test("Weekly CA", self.cgl, tab_key="cabc12")
        plain = self.make_test("Plain full", self.cgl)
        self.client.force_authenticate(self.stu)
        d = self.hub().data
        self.assertEqual([c["id"] for c in d["custom_tests"]["cabc12"]], [e.public_id])
        self.assertEqual([c["id"] for c in d["full_tests"]], [plain.public_id])
        self.assertEqual(d["progress"]["total"], 2)  # still counted once

    def test_a_shared_test_goes_by_type_in_the_other_exam(self):
        e = self.make_test("Weekly CA", self.cgl, tab_key="cabc12")
        e.also_in.add(self.chsl)
        self.client.force_authenticate(self.stu)
        d = self.hub(self.chsl).data
        self.assertEqual([c["id"] for c in d["full_tests"]], [e.public_id])
        self.assertEqual(d["custom_tests"], {})

    def test_a_deleted_tab_sends_its_tests_back_to_their_type_tab(self):
        e = self.make_test("Weekly CA", self.cgl, tab_key="cabc12")
        self.client.patch(f"/api/exam-categories/{self.cgl.id}/", {"tabs": [{"key": "full", "label": "Full tests"}]}, format="json")
        self.client.force_authenticate(self.stu)
        d = self.hub().data
        self.assertEqual([c["id"] for c in d["full_tests"]], [e.public_id])
        self.assertEqual(d["custom_tests"], {})

    def test_the_admin_can_put_a_test_in_a_tab_only_of_its_own_exam(self):
        e = self.make_test("T", self.cgl, status="draft")
        url = f"/api/exams/{e.id}/"
        ok = self.client.patch(url, {"tab_key": "cabc12"}, format="json")
        self.assertEqual(ok.status_code, 200, ok.content)
        self.assertEqual(ok.data["tab_key"], "cabc12")
        for bad in ("cother1", "full"):
            r = self.client.patch(url, {"tab_key": bad}, format="json")
            self.assertEqual(r.status_code, 400, bad)
            self.assertIn("tab_key", r.data)
        self.assertEqual(self.client.patch(url, {"tab_key": ""}, format="json").data["tab_key"], "")
        e.refresh_from_db()
        self.assertEqual(e.tab_key, "")

    def test_a_test_without_an_exam_cannot_have_a_tab(self):
        e = self.make_test("T", None, status="draft")
        self.assertEqual(self.client.patch(f"/api/exams/{e.id}/", {"tab_key": "cabc12"}, format="json").status_code, 400)
