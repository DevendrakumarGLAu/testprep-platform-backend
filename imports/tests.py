import csv
import io
from unittest import mock

from django.core.files.uploadedfile import SimpleUploadedFile
from rest_framework.test import APITestCase

from accounts.models import Role, User
from questionbank.models import Question, QuestionOption, Subject, Topic

from . import parsers, pipeline
from .models import ImportJob, ImportRow

PW = "Str0ng!pass99"
HEADERS = ["Question", "Option A", "Option B", "Option C", "Option D", "Correct Answer", "Explanation",
           "Subject", "Topic", "Difficulty", "Marks", "Negative Marks", "Tags"]


def csv_bytes(rows, headers=HEADERS, delimiter=","):
    out = io.StringIO()
    w = csv.writer(out, delimiter=delimiter)
    w.writerow(headers)
    w.writerows(rows)
    return ("﻿" + out.getvalue()).encode("utf-8")


GOOD = ["What is 2+2?", "3", "4", "5", "6", "B", "Basic sum", "Maths", "Arithmetic", "easy", "2", "0.5", "numbers;basic"]


class ImportBase(APITestCase):
    def setUp(self):
        self.admin = User.objects.create_user(username="adm", email="a@x.com", password=PW, role=Role.ADMIN)
        self.student = User.objects.create_user(username="stu", email="s@x.com", password=PW, role=Role.STUDENT)
        self.maths = Subject.objects.create(name="Maths")
        self.topic = Topic.objects.create(subject=self.maths, name="Arithmetic")
        self.client.force_authenticate(self.admin)

    def upload(self, name, data, ctype="application/octet-stream"):
        return self.client.post("/api/imports/", {"file": SimpleUploadedFile(name, data, content_type=ctype)}, format="multipart")

    def do_import(self, name, data, defaults=None, mapping=None, create_missing=False):
        r = self.upload(name, data)
        self.assertEqual(r.status_code, 201, r.content)
        jid = r.data["id"]
        body = {"defaults": defaults or {}, "create_missing": create_missing}
        if mapping:
            body["mapping"] = mapping
        p = self.client.post(f"/api/imports/{jid}/process/", body, format="json")
        self.assertEqual(p.status_code, 200, p.content)
        return jid, p.data

    def rows(self, jid, **q):
        qs = "&".join(f"{k}={v}" for k, v in q.items())
        return self.client.get(f"/api/imports/{jid}/rows/?{qs}").data["results"]


class UploadTests(ImportBase):
    def test_permissions(self):
        self.client.force_authenticate(self.student)
        self.assertEqual(self.client.get("/api/imports/").status_code, 403)
        self.assertEqual(self.client.get("/api/imports/template/").status_code, 403)
        self.client.force_authenticate(None)
        self.assertEqual(self.client.get("/api/imports/sources/").status_code, 401)

    def test_sources_list_marks_website_unavailable(self):
        d = {s["id"]: s for s in self.client.get("/api/imports/sources/").data}
        self.assertTrue(d["csv"]["available"] and d["excel"]["available"] and d["pdf"]["available"])
        self.assertFalse(d["web"]["available"])
        r = self.client.post("/api/imports/", {"source": "web"}, format="json")
        self.assertEqual(r.status_code, 501)

    def test_template_downloads_and_roundtrips(self):
        r = self.client.get("/api/imports/template/")
        self.assertEqual(r.status_code, 200)
        self.assertIn("attachment", r["Content-Disposition"])
        data = r.content
        self.assertTrue(data.startswith(b"\xef\xbb\xbf"))
        # The template's own example row is a valid import (after choosing a subject).
        Subject.objects.create(name="Geography")
        Topic.objects.create(subject=Subject.objects.get(name="Geography"), name="Capitals")
        jid, job = self.do_import("template.csv", data)
        self.assertEqual((job["valid_rows"], job["invalid_rows"]), (1, 0))

    def test_xlsx_template(self):
        r = self.client.get("/api/imports/template/?file=xlsx")
        self.assertEqual(r.status_code, 200)
        headers, rows, _ = parsers.parse_xlsx(r.content)
        self.assertEqual(headers[0], "question")
        self.assertEqual(len(rows), 1)

    def test_rejects_bad_files(self):
        for name, data, msg in [
            ("x.txt", b"hello", "Unsupported"),
            ("empty.csv", b"", "empty"),
            ("latin.csv", "Question\nCaf\xe9".encode("latin-1"), "UTF-8"),
            ("header.csv", csv_bytes([]), "no questions"),
            ("fake.xlsx", b"not a zip", "valid .xlsx"),
            ("fake.xls", b"not an xls", "valid .xls"),
            ("fake.pdf", b"not a pdf", "PDF"),
        ]:
            r = self.upload(name, data)
            self.assertEqual(r.status_code, 400, name)
            self.assertIn(msg.lower(), str(r.data["file"]).lower(), name)

    def test_a_file_larger_than_the_old_5_mb_limit_is_accepted(self):
        row = ["Padded question? " + "x" * 5000, "a", "b", "c", "d", "A", "", "Maths", "", "easy", "1", "0", ""]
        data = csv_bytes([row] * 1500)
        self.assertGreater(len(data), 5 * 1024 * 1024)
        r = self.upload("large.csv", data)
        self.assertEqual(r.status_code, 201, r.content)
        self.assertEqual(r.data["total_rows"], 1500)

    def test_too_large_and_too_many_rows(self):
        with mock.patch.object(parsers, "MAX_BYTES", 1024):  # a tiny limit keeps the test light
            self.assertEqual(self.upload("big.csv", b"a" * 1025).status_code, 400)
        rows = [GOOD] * (parsers.MAX_ROWS + 1)
        self.assertEqual(self.upload("many.csv", csv_bytes(rows)).status_code, 400)

    def test_auto_mapping_and_other_delimiters(self):
        r = self.upload("semi.csv", csv_bytes([GOOD], delimiter=";"))
        m = r.data["mapping"]
        self.assertEqual(m["question"], "Question")
        self.assertEqual(m["options"], ["Option A", "Option B", "Option C", "Option D"])
        self.assertEqual(m["correct"], "Correct Answer")
        self.assertEqual(m["negative_marks"], "Negative Marks")
        self.assertEqual(len(r.data["sample"]), 1)

    def test_bare_letter_headers_map_as_options(self):
        m = pipeline.guess_mapping(["Q", "A", "B", "C", "D", "Ans"])
        self.assertEqual((m["question"], m["correct"], m["options"]), ("Q", "Ans", ["A", "B", "C", "D"]))


class ValidationTests(ImportBase):
    def test_valid_row_normalised(self):
        jid, job = self.do_import("q.csv", csv_bytes([GOOD]))
        row = self.rows(jid)[0]
        n = row["normalized"]
        self.assertEqual(row["status"], "valid")
        self.assertTrue(row["selected"])
        self.assertEqual((n["correct_label"], n["difficulty"], n["marks"], n["negative_marks"]), ("B", "easy", "2", "0.5"))
        self.assertEqual(n["tags"], ["numbers", "basic"])
        self.assertEqual((n["subject_id"], n["topic_id"]), (self.maths.id, self.topic.id))

    def test_row_level_errors(self):
        bad = {
            "no question": ["", "a", "b", "c", "d", "A", "", "Maths", "", "", "", "", ""],
            "one option": ["Q1 text", "only", "", "", "", "A", "", "Maths", "", "", "", "", ""],
            "dup options": ["Q2 text", "x", "X", "", "", "A", "", "Maths", "", "", "", "", ""],
            "no answer": ["Q3 text", "a", "b", "", "", "", "", "Maths", "", "", "", "", ""],
            "bad answer": ["Q4 text", "a", "b", "", "", "Z", "", "Maths", "", "", "", "", ""],
            "multi answer": ["Q5 text", "a", "b", "c", "", "A,B", "", "Maths", "", "", "", "", ""],
            "bad subject": ["Q6 text", "a", "b", "", "", "A", "", "Nope", "", "", "", "", ""],
            "bad topic": ["Q7 text", "a", "b", "", "", "A", "", "Maths", "Ghost", "", "", "", ""],
            "bad difficulty": ["Q8 text", "a", "b", "", "", "A", "", "Maths", "", "extreme", "", "", ""],
            "bad marks": ["Q9 text", "a", "b", "", "", "A", "", "Maths", "", "", "abc", "", ""],
            "zero marks": ["Q10 text", "a", "b", "", "", "A", "", "Maths", "", "", "0", "", ""],
        }
        jid, job = self.do_import("bad.csv", csv_bytes(list(bad.values())))
        self.assertEqual((job["valid_rows"], job["invalid_rows"]), (0, len(bad)))
        for name, row in zip(bad, self.rows(jid)):
            self.assertEqual(row["status"], "invalid", name)
            self.assertTrue(row["errors"], name)
            self.assertFalse(row["selected"])

    def test_answer_formats(self):
        rows = [
            ["Letter q", "a", "b", "", "", "b", "", "Maths", "", "", "", "", ""],
            ["Number q", "a", "b", "", "", "2", "", "Maths", "", "", "", "", ""],
            ["Text q", "alpha", "beta", "", "", "Beta", "", "Maths", "", "", "", "", ""],
            ["Dotted q", "a", "b", "", "", "A.", "", "Maths", "", "", "", "", ""],
        ]
        jid, _ = self.do_import("a.csv", csv_bytes(rows))
        self.assertEqual([r["normalized"]["correct_label"] for r in self.rows(jid)], ["B", "B", "B", "A"])

    def test_defaults_fill_missing_cells_and_negative_sign_is_a_deduction(self):
        row = ["Defaults q", "a", "b", "", "", "A", "", "", "", "", "", "-0.25", ""]
        jid, _ = self.do_import("d.csv", csv_bytes([row]), defaults={"subject": self.maths.id, "topic": self.topic.id, "difficulty": "hard", "marks": 3})
        n = self.rows(jid)[0]["normalized"]
        self.assertEqual((n["subject"], n["topic"], n["difficulty"], n["marks"], n["negative_marks"]), ("Maths", "Arithmetic", "hard", "3", "0.25"))

    def test_mapping_can_be_changed(self):
        data = csv_bytes([["Capital of France?", "Paris", "Rome", "1"]], headers=["Prompt", "Alpha", "Beta", "Key"])
        jid, job = self.do_import("m.csv", data, defaults={"subject": self.maths.id},
                            mapping={"question": "Prompt", "options": ["Alpha", "Beta"], "correct": "Key"})
        self.assertEqual(job["valid_rows"], 1)

    def test_unknown_column_in_mapping_rejected(self):
        r = self.upload("x.csv", csv_bytes([GOOD]))
        p = self.client.post(f"/api/imports/{r.data['id']}/process/", {"mapping": {"question": "Nope", "options": ["A", "B"]}}, format="json")
        self.assertEqual(p.status_code, 400)

    def test_create_missing_subject_and_topic_on_commit(self):
        row = ["Q about rivers", "a", "b", "", "", "A", "", "Geography", "Rivers", "", "", "", ""]
        jid, job = self.do_import("g.csv", csv_bytes([row]), create_missing=True)
        self.assertEqual(job["valid_rows"], 1)
        self.assertFalse(Subject.objects.filter(name="Geography").exists())  # nothing created until commit
        self.client.post(f"/api/imports/{jid}/commit/")
        q = Question.objects.get(text="Q about rivers")
        self.assertEqual((q.subject.name, q.topic.name), ("Geography", "Rivers"))


class DuplicateTests(ImportBase):
    def test_duplicates_against_bank_and_inside_file(self):
        q = Question.objects.create(text="What is 2+2?", subject=self.maths)
        QuestionOption.objects.create(question=q, text="4", is_correct=True)
        QuestionOption.objects.create(question=q, text="5", is_correct=False)
        rows = [
            ["what is 2 + 2", "3", "4", "", "", "B", "", "Maths", "", "", "", "", ""],          # exact (normalised)
            ["What is the sum of 2 and 2?", "3", "4", "", "", "B", "", "Maths", "", "", "", "", ""],  # different enough
            ["Name the largest planet", "a", "b", "", "", "A", "", "Maths", "", "", "", "", ""],
            ["Name the largest planet?", "a", "b", "", "", "A", "", "Maths", "", "", "", "", ""],   # repeated in the file
        ]
        jid, job = self.do_import("dup.csv", csv_bytes(rows))
        r = self.rows(jid)
        self.assertEqual([x["status"] for x in r], ["duplicate", "valid", "valid", "duplicate"])
        self.assertEqual(r[0]["duplicate_of"], q.id)
        self.assertTrue(r[0]["duplicate_info"]["exact"])
        self.assertTrue(r[3]["duplicate_info"]["in_file"])
        self.assertFalse(r[0]["selected"])  # duplicates are never pre-selected
        self.assertEqual(job["duplicate_rows"], 2)

    def test_near_duplicate_flagged(self):
        q = Question.objects.create(text="Which planet is known as the Red Planet in our solar system", subject=self.maths)
        QuestionOption.objects.create(question=q, text="Mars", is_correct=True)
        QuestionOption.objects.create(question=q, text="Venus", is_correct=False)
        row = ["Which planet is known as the Red Planet in our solar system?!", "Mars", "Venus", "", "", "A", "", "Maths", "", "", "", "", ""]
        jid, _ = self.do_import("n.csv", csv_bytes([row]))
        self.assertEqual(self.rows(jid)[0]["status"], "duplicate")


class ReviewAndCommitTests(ImportBase):
    def make(self):
        rows = [
            GOOD,
            ["Second question text", "a", "b", "", "", "A", "", "Maths", "", "", "", "", ""],
            ["Broken question", "a", "", "", "", "A", "", "Maths", "", "", "", "", ""],
        ]
        return self.do_import("r.csv", csv_bytes(rows))

    def test_selective_import_and_summary(self):
        jid, job = self.make()
        rows = self.rows(jid)
        self.assertEqual([r["selected"] for r in rows], [True, True, False])
        # Deselect the first row, then import.
        self.client.patch(f"/api/imports/{jid}/rows/{rows[0]['id']}/", {"selected": False}, format="json")
        r = self.client.post(f"/api/imports/{jid}/commit/")
        self.assertEqual(r.status_code, 200, r.content)
        self.assertEqual((r.data["imported_count"], r.data["skipped_count"], r.data["failed_count"]), (1, 2, 0))
        self.assertEqual(r.data["status"], "imported")
        self.assertFalse(Question.objects.filter(text="What is 2+2?").exists())
        q = Question.objects.get(text="Second question text")
        self.assertEqual((q.source, q.source_info), ("csv", "r.csv row 3"))
        self.assertEqual(q.options.count(), 2)
        self.assertTrue(q.options.get(text="a").is_correct)

    def test_imported_question_has_all_fields(self):
        jid, _ = self.make()
        self.client.post(f"/api/imports/{jid}/commit/")
        q = Question.objects.get(text="What is 2+2?")
        self.assertEqual((q.difficulty, str(q.marks), str(q.negative_marks), q.topic.name), ("easy", "2.00", "0.50", "Arithmetic"))
        self.assertEqual(sorted(t.name for t in q.tags.all()), ["basic", "numbers"])
        self.assertEqual(q.explanation, "Basic sum")
        self.assertEqual(q.created_by, self.admin)

    def test_commit_twice_and_empty_selection_rejected(self):
        jid, _ = self.make()
        self.client.post(f"/api/imports/{jid}/select/", {"mode": "none"}, format="json")
        self.assertEqual(self.client.post(f"/api/imports/{jid}/commit/").status_code, 400)
        self.client.post(f"/api/imports/{jid}/select/", {"mode": "valid"}, format="json")
        self.assertEqual(self.client.post(f"/api/imports/{jid}/commit/").status_code, 200)
        self.assertEqual(self.client.post(f"/api/imports/{jid}/commit/").status_code, 400)
        self.assertEqual(Question.objects.count(), 2)  # no double import
        self.assertEqual(self.client.delete(f"/api/imports/{jid}/").status_code, 409)

    def test_invalid_rows_cannot_be_selected(self):
        jid, _ = self.make()
        bad = self.rows(jid)[2]
        self.client.patch(f"/api/imports/{jid}/rows/{bad['id']}/", {"selected": True}, format="json")
        self.assertFalse(ImportRow.objects.get(pk=bad["id"]).selected)

    def test_duplicate_can_be_imported_when_chosen(self):
        q = Question.objects.create(text="Second question text", subject=self.maths)
        QuestionOption.objects.create(question=q, text="a", is_correct=True)
        QuestionOption.objects.create(question=q, text="b", is_correct=False)
        jid, _ = self.make()
        dup = next(r for r in self.rows(jid) if r["status"] == "duplicate")
        self.client.patch(f"/api/imports/{jid}/rows/{dup['id']}/", {"selected": True}, format="json")
        r = self.client.post(f"/api/imports/{jid}/commit/")
        self.assertEqual(r.data["imported_count"], 2)
        self.assertEqual(Question.objects.filter(text="Second question text").count(), 2)

    def test_fix_a_row_then_import(self):
        jid, job = self.make()
        bad = self.rows(jid)[2]
        r = self.client.patch(f"/api/imports/{jid}/rows/{bad['id']}/", {"fixes": {"question": "Fixed question text"}}, format="json")
        self.assertEqual(r.data["row"]["status"], "invalid")  # still only one option
        self.assertEqual(r.data["job"]["invalid_rows"], 1)
        # Fix the answer on a valid row by choosing another option.
        good = self.rows(jid)[1]
        r = self.client.patch(f"/api/imports/{jid}/rows/{good['id']}/", {"fixes": {"correct": "B"}}, format="json")
        self.assertEqual(r.data["row"]["normalized"]["correct_label"], "B")

    def test_failed_row_report_csv(self):
        jid, _ = self.make()
        self.client.post(f"/api/imports/{jid}/commit/")
        r = self.client.get(f"/api/imports/{jid}/failed-report/")
        self.assertEqual(r.status_code, 200)
        self.assertIn("text/csv", r["Content-Type"])
        lines = list(csv.reader(io.StringIO(r.content.decode("utf-8-sig"))))
        self.assertEqual(lines[0][:3], ["row", "problem", "Question"])
        self.assertEqual(len(lines), 2)  # header + the one invalid row
        self.assertEqual(lines[1][0], "4")
        self.assertIn("two options", lines[1][1])
        self.assertEqual(lines[1][2], "Broken question")

    def test_failed_report_neutralises_formulas(self):
        row = ["=HYPERLINK(\"http://evil\")", "a", "", "", "", "A", "", "Maths", "", "", "", "", ""]
        jid, _ = self.do_import("f.csv", csv_bytes([row]))
        text = self.client.get(f"/api/imports/{jid}/failed-report/").content.decode("utf-8-sig")
        self.assertIn("'=HYPERLINK", text)

    def test_row_failing_at_commit_does_not_block_others(self):
        jid, _ = self.make()
        # Archive the subject after review so saving the first two rows fails validation.
        self.maths.is_active = False
        self.maths.save()
        r = self.client.post(f"/api/imports/{jid}/commit/")
        self.assertEqual((r.data["imported_count"], r.data["failed_count"]), (0, 2))
        self.assertTrue(ImportRow.objects.filter(job_id=jid, result="failed").exclude(result_message="").exists())

    def test_job_list_and_detail(self):
        jid, _ = self.make()
        self.assertEqual(self.client.get("/api/imports/").data["count"], 1)
        d = self.client.get(f"/api/imports/{jid}/").data
        self.assertEqual((d["total_rows"], d["valid_rows"], d["invalid_rows"]), (3, 2, 1))

    def test_discard_before_import(self):
        jid, _ = self.make()
        self.assertEqual(self.client.delete(f"/api/imports/{jid}/").status_code, 204)
        self.assertEqual(ImportJob.objects.count(), 0)


class ExcelTests(ImportBase):
    def build(self, rows, headers=HEADERS, extra_sheet=False):
        from openpyxl import Workbook

        wb = Workbook()
        ws = wb.active
        ws.append(headers)
        for r in rows:
            ws.append(r)
        if extra_sheet:
            wb.create_sheet("Other").append(["ignored"])
        buf = io.BytesIO()
        wb.save(buf)
        return buf.getvalue()

    def test_xlsx_import_goes_through_same_pipeline(self):
        row = list(GOOD)
        row[10], row[11] = 2, 0.5  # real numbers, as Excel stores them
        jid, job = self.do_import("q.xlsx", self.build([row], extra_sheet=True))
        self.assertEqual(job["source_type"], "excel")
        self.assertEqual(job["valid_rows"], 1)
        self.assertTrue(any("first sheet" in w for w in job["warnings"]))
        n = self.rows(jid)[0]["normalized"]
        self.assertEqual((n["marks"], n["negative_marks"]), ("2", "0.5"))
        self.client.post(f"/api/imports/{jid}/commit/")
        self.assertEqual(Question.objects.get(text="What is 2+2?").source, "excel")

    def test_formulas_are_not_evaluated_and_blank_rows_skipped(self):
        data = self.build([["=1+1", "a", "b", "", "", "A", "", "Maths", "", "", "", "", ""], [None] * 13])
        jid, job = self.do_import("f.xlsx", data)
        self.assertEqual(job["total_rows"], 1)

    def test_xls_dispatch(self):
        # Legacy .xls goes to xlrd; a corrupt file is reported cleanly (covered in test_rejects_bad_files).
        self.assertEqual(parsers.detect_source("old.XLS"), "excel")


class PdfTests(ImportBase):
    TEXT = """
    Sample Question Paper

    1. What is the capital of France?
    A. Berlin
    B. Paris
    C. Rome
    D. Madrid
    Answer: B
    Explanation: Paris has been the capital for centuries
    and is its largest city.

    2) Which planet is closest to the Sun
       and has no moons?
    (a) Venus
    (b) Mercury
    (c) Mars
    Ans: b

    Q3. Water boils at 100 degrees Celsius at sea level.
    A) True
    B) False
    Answer: A
    """

    def test_pdf_text_parser(self):
        headers, rows, warnings = parsers.parse_pdf_text(self.TEXT)
        self.assertEqual(len(rows), 3)
        self.assertEqual(rows[0]["question"], "What is the capital of France?")
        self.assertEqual((rows[0]["option_b"], rows[0]["correct_answer"]), ("Paris", "B"))
        self.assertIn("largest city", rows[0]["explanation"])
        self.assertEqual(rows[1]["question"], "Which planet is closest to the Sun and has no moons?")  # wrapped line joined
        self.assertEqual((rows[1]["option_b"], rows[1]["correct_answer"]), ("Mercury", "b"))
        self.assertEqual(rows[2]["option_a"], "True")
        self.assertEqual(warnings, [])

    def test_pdf_without_answers_warns_and_rows_need_fixing(self):
        headers, rows, warnings = parsers.parse_pdf_text("1. Question one here\nA. yes\nB. no\n\n2. Question two here\nA. x\nB. y\n")
        self.assertTrue(any("Answer:" in w for w in warnings))
        self.assertEqual(rows[0]["correct_answer"], "")

    def test_unrecognised_text_is_an_error(self):
        with self.assertRaises(parsers.ParseError):
            parsers.parse_pdf_text("Just a paragraph of prose with no numbered questions at all.")

    def test_image_only_pdf_is_rejected_with_clear_message(self):
        from pypdf import PdfWriter

        w = PdfWriter()
        w.add_blank_page(width=200, height=200)
        buf = io.BytesIO()
        w.write(buf)
        r = self.upload("scan.pdf", buf.getvalue())
        self.assertEqual(r.status_code, 400)
        self.assertIn("OCR", str(r.data["file"]))

    def test_pdf_rows_use_same_pipeline_and_can_be_fixed(self):
        headers, rows, _ = parsers.parse_pdf_text("1. Question without a key\nA. yes\nB. no\n")
        job = ImportJob.objects.create(created_by=self.admin, source_type="pdf", filename="p.pdf", headers=headers,
                                       mapping=pipeline.guess_mapping(headers))
        ImportRow.objects.bulk_create(ImportRow(job=job, row_number=i + 1, raw=r) for i, r in enumerate(rows))
        p = self.client.post(f"/api/imports/{job.id}/process/", {"defaults": {"subject": self.maths.id}}, format="json")
        self.assertEqual((p.data["valid_rows"], p.data["invalid_rows"]), (0, 1))
        row = self.rows(job.id)[0]
        self.assertIn("Correct answer is missing.", row["errors"])
        r = self.client.patch(f"/api/imports/{job.id}/rows/{row['id']}/", {"fixes": {"correct": "A"}}, format="json")
        self.assertEqual(r.data["row"]["status"], "valid")
        self.assertTrue(r.data["row"]["selected"])
        c = self.client.post(f"/api/imports/{job.id}/commit/")
        self.assertEqual(c.data["imported_count"], 1)
        self.assertEqual(Question.objects.get(text="Question without a key").source, "pdf")
