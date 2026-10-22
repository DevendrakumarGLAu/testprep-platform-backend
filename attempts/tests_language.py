from exams.models import Exam, ExamQuestion, ExamSection
from questionbank.models import Question, QuestionOption

from .models import AttemptQuestion
from .tests import EngineBase


class BilingualQuestionTests(EngineBase):
    """Questions can be written in English, Hindi or both; the student sees the language they choose."""

    def body(self, **over):
        data = {
            "text": "What is 2+2?", "text_hi": "2+2 कितना होता है?", "subject": self.subject.id, "marks": "1", "negative_marks": "0",
            "explanation_hi": "जोड़",
            "options": [
                {"text": "4", "text_hi": "चार", "is_correct": True},
                {"text": "5", "text_hi": "पाँच", "is_correct": False},
            ],
        }
        data.update(over)
        return data

    def post(self, **over):
        self.client.force_authenticate(self.admin)
        return self.client.post("/api/questions/", self.body(**over), format="json")

    def test_both_languages_are_saved_and_reported(self):
        r = self.post()
        self.assertEqual(r.status_code, 201, r.content)
        self.assertEqual(r.data["languages"], ["en", "hi"])
        self.assertEqual(r.data["options"][0]["text_hi"], "चार")

    def test_hindi_only_is_allowed(self):
        r = self.post(text="", options=[{"text_hi": "चार", "is_correct": True}, {"text_hi": "पाँच", "is_correct": False}])
        self.assertEqual(r.status_code, 201, r.content)
        self.assertEqual(r.data["languages"], ["hi"])

    def test_english_only_is_allowed(self):
        r = self.post(text_hi="", options=[{"text": "4", "is_correct": True}, {"text": "5", "is_correct": False}])
        self.assertEqual(r.status_code, 201, r.content)
        self.assertEqual(r.data["languages"], ["en"])

    def test_at_least_one_language_is_required(self):
        r = self.post(text="", text_hi="", options=[{"is_correct": True}, {"is_correct": False}])
        self.assertEqual(r.status_code, 400)
        self.assertIn("text", r.data)

    def test_a_started_language_must_be_finished(self):
        half = self.post(options=[{"text": "4", "text_hi": "चार", "is_correct": True}, {"text": "5", "text_hi": "", "is_correct": False}])
        self.assertEqual(half.status_code, 400)
        self.assertIn("text_hi", half.data)
        no_stem = self.post(text="", options=[{"text": "4", "text_hi": "चार", "is_correct": True}, {"text": "5", "text_hi": "पाँच", "is_correct": False}])
        self.assertEqual(no_stem.status_code, 400)
        self.assertIn("text", no_stem.data)

    def test_option_must_still_have_one_correct(self):
        r = self.post(options=[{"text": "4", "text_hi": "चार", "is_correct": False}, {"text": "5", "text_hi": "पाँच", "is_correct": False}])
        self.assertEqual(r.status_code, 400)

    def test_adding_hindi_to_an_english_question(self):
        created = self.post(text_hi="", options=[{"text": "4", "is_correct": True}, {"text": "5", "is_correct": False}])
        ids = [o["id"] for o in created.data["options"]]
        r = self.client.put(f"/api/questions/{created.data['id']}/", self.body(options=[
            {"id": ids[0], "text": "4", "text_hi": "चार", "is_correct": True},
            {"id": ids[1], "text": "5", "text_hi": "पाँच", "is_correct": False},
        ]), format="json")
        self.assertEqual(r.status_code, 200, r.content)
        self.assertEqual(r.data["languages"], ["en", "hi"])

    def test_student_gets_both_texts_and_the_languages_on_offer(self):
        q = Question.objects.create(text="Capital?", text_hi="राजधानी?", subject=self.subject, marks=2)
        QuestionOption.objects.create(question=q, text="Delhi", text_hi="दिल्ली", is_correct=True, order=0)
        QuestionOption.objects.create(question=q, text="Pune", text_hi="पुणे", is_correct=False, order=1)
        exam = Exam.objects.create(title="Bi", duration_minutes=10, status=Exam.Status.PUBLISHED, max_attempts=0)
        sec = ExamSection.objects.create(exam=exam, name="S", order=0)
        ExamQuestion.objects.create(section=sec, question=q, order=0, marks=2, negative_marks=0, question_version=q.version)
        self.login()
        detail = self.client.get(f"/api/student/exams/{exam.public_id}/")
        self.assertEqual(detail.data["languages"], ["en", "hi"])
        a = self.start(exam)
        r = self.client.get(f"/api/student/attempts/{a}/")
        self.assertEqual(r.data["languages"], ["en", "hi"])
        first = r.data["sections"][0]["questions"][0]
        self.assertEqual((first["text"], first["text_hi"]), ("Capital?", "राजधानी?"))
        self.assertEqual({o["text_hi"] for o in first["options"]}, {"दिल्ली", "पुणे"})
        self.assertNotIn("is_correct", first["options"][0])
        # the snapshot keeps Hindi even if the bank changes later
        Question.objects.filter(pk=q.pk).update(text_hi="बदला")
        self.assertEqual(AttemptQuestion.objects.get(attempt__public_id=a).text_hi, "राजधानी?")

    def test_english_only_test_offers_only_english(self):
        self.login()
        self.assertEqual(self.client.get(f"/api/student/exams/{self.exam.public_id}/").data["languages"], ["en"])
        a = self.start()
        self.assertEqual(self.client.get(f"/api/student/attempts/{a}/").data["languages"], ["en"])

    def test_result_review_carries_hindi(self):
        q = Question.objects.create(text="", text_hi="प्रश्न", explanation_hi="व्याख्या", subject=self.subject, marks=2)
        QuestionOption.objects.create(question=q, text_hi="सही", is_correct=True, order=0)
        QuestionOption.objects.create(question=q, text_hi="गलत", is_correct=False, order=1)
        exam = Exam.objects.create(title="Hi", duration_minutes=10, status=Exam.Status.PUBLISHED, max_attempts=0)
        sec = ExamSection.objects.create(exam=exam, name="S", order=0)
        ExamQuestion.objects.create(section=sec, question=q, order=0, marks=2, negative_marks=0, question_version=q.version)
        self.login()
        a = self.start(exam)
        self.assertEqual(self.client.get(f"/api/student/attempts/{a}/").data["languages"], ["hi"])
        self.client.post(f"/api/student/attempts/{a}/submit/")
        res = self.client.get(f"/api/student/attempts/{a}/result/")
        self.assertEqual(res.status_code, 200, res.content)
        item = res.data["questions"][0]
        self.assertEqual((item["text_hi"], item["explanation_hi"]), ("प्रश्न", "व्याख्या"))
