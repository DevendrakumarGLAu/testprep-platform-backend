from questionbank.models import Question

from .tests import ImportBase, csv_bytes

HI_HEADERS = ["question_hi", "option_a_hi", "option_b_hi", "option_c_hi", "option_d_hi", "correct_answer", "subject"]
BOTH = ["question", "option_a", "option_b", "option_c", "option_d",
        "question_hi", "option_a_hi", "option_b_hi", "option_c_hi", "option_d_hi", "correct_answer", "subject"]


class HindiImportTests(ImportBase):
    def commit(self, jid):
        r = self.client.post(f"/api/imports/{jid}/commit/", {}, format="json")
        self.assertEqual(r.status_code, 200, r.content)
        self.assertEqual(r.data["failed_count"], 0)

    def test_hindi_columns_are_recognised_automatically(self):
        r = self.upload("hi.csv", csv_bytes([["2+2 कितना है?", "3", "4", "5", "6", "B", "Maths"]], headers=HI_HEADERS))
        m = r.data["mapping"]
        self.assertEqual(m["question_hi"], "question_hi")
        self.assertEqual(m["options_hi"], ["option_a_hi", "option_b_hi", "option_c_hi", "option_d_hi"])
        self.assertEqual(m["options"], [])

    def test_hindi_only_file_imports_as_a_hindi_question(self):
        rows = [["भारत की राजधानी क्या है?", "मुंबई", "नई दिल्ली", "कोलकाता", "चेन्नई", "B", "Maths"]]
        jid, _ = self.do_import("hi.csv", csv_bytes(rows, headers=HI_HEADERS))
        self.assertEqual([x["status"] for x in self.rows(jid)], ["valid"], self.rows(jid))
        self.commit(jid)
        q = Question.objects.get()
        self.assertEqual((q.text, q.text_hi), ("", "भारत की राजधानी क्या है?"))
        self.assertEqual([(o.text_hi, o.is_correct) for o in q.options.all()][1], ("नई दिल्ली", True))

    def test_bilingual_file_fills_both_languages(self):
        rows = [["Capital of India?", "Mumbai", "New Delhi", "Kolkata", "Chennai",
                 "भारत की राजधानी?", "मुंबई", "नई दिल्ली", "कोलकाता", "चेन्नई", "B", "Maths"]]
        jid, _ = self.do_import("both.csv", csv_bytes(rows, headers=BOTH))
        self.assertEqual([x["status"] for x in self.rows(jid)], ["valid"], self.rows(jid))
        self.commit(jid)
        q = Question.objects.get()
        self.assertEqual((q.text, q.text_hi), ("Capital of India?", "भारत की राजधानी?"))
        opts = list(q.options.all())
        self.assertEqual((opts[1].text, opts[1].text_hi, opts[1].is_correct), ("New Delhi", "नई दिल्ली", True))

    def test_answer_may_be_given_as_hindi_option_text(self):
        rows = [["प्रश्न?", "क", "ख", "ग", "घ", "ग", "Maths"]]
        jid, _ = self.do_import("hi.csv", csv_bytes(rows, headers=HI_HEADERS))
        self.assertEqual([x["status"] for x in self.rows(jid)], ["valid"], self.rows(jid))

    def test_half_finished_language_is_rejected(self):
        rows = [["Q?", "a", "b", "c", "d", "प्रश्न?", "क", "ख", "", "घ", "A", "Maths"]]
        jid, _ = self.do_import("both.csv", csv_bytes(rows, headers=BOTH))
        self.assertEqual([x["status"] for x in self.rows(jid)], ["invalid"])
        self.assertIn("Hindi version is incomplete", " ".join(self.rows(jid)[0]["errors"]))

    def test_a_row_with_no_question_in_either_language_is_rejected(self):
        rows = [["", "a", "b", "c", "d", "", "", "", "", "", "A", "Maths"]]
        jid, _ = self.do_import("both.csv", csv_bytes(rows, headers=BOTH))
        self.assertEqual([x["status"] for x in self.rows(jid)], ["invalid"])
        self.assertIn("Question text is missing", " ".join(self.rows(jid)[0]["errors"]))

    def test_duplicate_hindi_question_is_found(self):
        rows = [["वही प्रश्न?", "क", "ख", "ग", "घ", "A", "Maths"]]
        jid, _ = self.do_import("hi.csv", csv_bytes(rows, headers=HI_HEADERS))
        self.commit(jid)
        jid2, _ = self.do_import("hi2.csv", csv_bytes(rows, headers=HI_HEADERS))
        self.assertEqual([x["status"] for x in self.rows(jid2)], ["duplicate"])


class ResumeTests(ImportBase):
    def test_committing_again_after_a_cut_off_import_does_not_duplicate(self):
        rows = [[f"प्रश्न {i}?", "क", "ख", "ग", "घ", "A", "Maths"] for i in range(3)]
        jid, _ = self.do_import("hi.csv", csv_bytes(rows, headers=HI_HEADERS))
        from .models import ImportJob, ImportRow
        self.assertEqual(self.client.post(f"/api/imports/{jid}/commit/", {}, format="json").status_code, 200)
        self.assertEqual(Question.objects.count(), 3)
        # Pretend the request died after two rows: job not finished, last row not marked.
        last = ImportRow.objects.filter(job_id=jid).order_by("row_number").last()
        last.result, last.question = "", None
        last.save()
        Question.objects.filter(text_hi=f"प्रश्न 2?").delete()
        ImportJob.objects.filter(pk=jid).update(status=ImportJob.Status.PROCESSED)
        r = self.client.post(f"/api/imports/{jid}/commit/", {}, format="json")
        self.assertEqual(r.status_code, 200, r.content)
        self.assertEqual(Question.objects.count(), 3)  # the two already imported are kept once, the missing one is added
        self.assertEqual(r.data["imported_count"], 3)


class BilingualPdfTextTests(ImportBase):
    def test_english_then_hindi_is_split_into_two_languages(self):
        from .parsers import split_languages
        en, hi = split_languages(
            "Who is the author of the 2024 Booker Prize novel 'Kairos'? 2024 के बुकर पुरस्कार उपन्यास 'काइरोस' (Kairos) के लेखक कौन हैं? "
            "[SSC Phase-14 (M) Exam 2026 17 Sept 2026 S3]", strip_tag=True)
        self.assertEqual(en, "Who is the author of the 2024 Booker Prize novel 'Kairos'?")
        self.assertTrue(hi.startswith("2024 के बुकर") and hi.endswith("कौन हैं?"))
        self.assertNotIn("SSC", hi)

    def test_options_in_one_language_or_both(self):
        from .parsers import split_languages
        self.assertEqual(split_languages("Han Kang हान कांग"), ("Han Kang", "हान कांग"))
        self.assertEqual(split_languages("Han Kang (हान कांग)"), ("Han Kang", "हान कांग"))
        self.assertEqual(split_languages("नई दिल्ली"), ("", "नई दिल्ली"))
        self.assertEqual(split_languages("New Delhi"), ("New Delhi", ""))

    def test_a_bilingual_pdf_becomes_english_and_hindi_columns(self):
        import os
        import pymupdf
        font = r"C:\Windows\Fonts\Nirmala.ttc"
        if not os.path.exists(font):
            self.skipTest("needs a Devanagari font to build the sample PDF")
        doc = pymupdf.open()
        page = doc.new_page()
        page.insert_font(fontname="hi", fontfile=font)
        for n, line in enumerate([
            "Q1. Capital of India? भारत की राजधानी क्या है?", "A. Mumbai मुंबई", "B. New Delhi नई दिल्ली",
            "C. Kolkata कोलकाता", "D. Chennai चेन्नई", "Answer: B",
        ]):
            page.insert_text((40, 80 + 22 * n), line, fontname="hi", fontsize=11)
        from .parsers import parse_pdf
        headers, rows, _ = parse_pdf(doc.tobytes())
        self.assertIn("question_hi", headers)
        self.assertEqual((rows[0]["question"], rows[0]["question_hi"]), ("Capital of India?", "भारत की राजधानी क्या है?"))
        self.assertEqual((rows[0]["option_b"], rows[0]["option_b_hi"]), ("New Delhi", "नई दिल्ली"))


class ImportDateTests(ImportBase):
    HEAD = ["question", "option_a", "option_b", "correct_answer", "subject", "date"]

    def rows_for(self, *dates):
        rows = [[f"Q number {i}?", "a", "b", "A", "Maths", d] for i, d in enumerate(dates)]
        jid, _ = self.do_import("d.csv", csv_bytes(rows, headers=self.HEAD))
        return jid, self.rows(jid)

    def test_date_column_is_recognised_and_read_in_several_formats(self):
        jid, rows = self.rows_for("2026-07-08", "08/07/2026", "8 Jul 2026", "2026-07-08 00:00:00")
        self.assertEqual([r["status"] for r in rows], ["valid"] * 4, rows)
        self.assertEqual({r["normalized"]["question_date"] for r in rows}, {"2026-07-08"})
        self.client.post(f"/api/imports/{jid}/commit/", {}, format="json")
        self.assertEqual({str(q.question_date) for q in Question.objects.all()}, {"2026-07-08"})

    def test_missing_date_stays_empty_and_a_bad_or_future_date_is_flagged(self):
        _, rows = self.rows_for("", "not a date", "2999-01-01")
        self.assertEqual([r["status"] for r in rows], ["valid", "invalid", "invalid"])
        self.assertIsNone(rows[0]["normalized"]["question_date"])
        self.assertIn("not valid", " ".join(rows[1]["errors"]))
        self.assertIn("future", " ".join(rows[2]["errors"]))
