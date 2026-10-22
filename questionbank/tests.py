import io
import shutil
import tempfile

from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import override_settings
from rest_framework.test import APITestCase

from accounts.models import Role, User

from .models import Question, QuestionOption, Subject, Topic

PW = "Str0ng!pass99"
MEDIA = tempfile.mkdtemp()


def user(name, role):
    return User.objects.create_user(username=name, email=f"{name}@x.com", password=PW, role=role)


def payload(subject, topic=None, **over):
    body = {
        "text": "What is the capital of France?",
        "subject": subject.id,
        "topic": topic.id if topic else None,
        "difficulty": "easy",
        "marks": "2",
        "negative_marks": "0.5",
        "explanation": "Paris is the capital.",
        "tags": ["Geography", "europe"],
        "options": [
            {"text": "Paris", "is_correct": True},
            {"text": "Rome", "is_correct": False},
            {"text": "Berlin", "is_correct": False},
        ],
    }
    body.update(over)
    return body


@override_settings(MEDIA_ROOT=MEDIA)
class QuestionBankTests(APITestCase):
    @classmethod
    def tearDownClass(cls):
        super().tearDownClass()
        shutil.rmtree(MEDIA, ignore_errors=True)

    def setUp(self):
        self.admin = user("adm", Role.ADMIN)
        self.student = user("stu", Role.STUDENT)
        self.subject = Subject.objects.create(name="Geography")
        self.topic = Topic.objects.create(subject=self.subject, name="Capitals")
        self.client.force_authenticate(self.admin)

    def create(self, **over):
        return self.client.post("/api/questions/", payload(self.subject, self.topic, **over), format="json")

    # --- permissions ---
    def test_students_and_anonymous_are_blocked(self):
        self.client.force_authenticate(self.student)
        for url in ("/api/subjects/", "/api/topics/", "/api/questions/", "/api/tags/"):
            self.assertEqual(self.client.get(url).status_code, 403, url)
        self.client.force_authenticate(None)
        self.assertEqual(self.client.get("/api/questions/").status_code, 401)

    # --- subjects / topics ---
    def test_subject_name_unique_case_insensitive(self):
        r = self.client.post("/api/subjects/", {"name": "geography"})
        self.assertEqual(r.status_code, 400)

    def test_referenced_subject_and_topic_cannot_be_deleted_but_can_be_archived(self):
        self.assertEqual(self.create().status_code, 201)
        self.assertEqual(self.client.delete(f"/api/topics/{self.topic.id}/").status_code, 409)
        self.assertEqual(self.client.delete(f"/api/subjects/{self.subject.id}/").status_code, 409)
        r = self.client.post(f"/api/subjects/{self.subject.id}/archive/")
        self.assertFalse(r.data["is_active"])
        self.assertTrue(Subject.objects.filter(id=self.subject.id).exists())

    def test_unreferenced_subject_can_be_deleted(self):
        s = Subject.objects.create(name="Empty")
        self.assertEqual(self.client.delete(f"/api/subjects/{s.id}/").status_code, 204)

    def test_topics_filter_by_subject_and_duplicate_name_rejected(self):
        other = Subject.objects.create(name="History")
        Topic.objects.create(subject=other, name="Ancient")
        r = self.client.get(f"/api/topics/?subject={self.subject.id}")
        self.assertEqual([t["name"] for t in r.data["results"]], ["Capitals"])
        r = self.client.post("/api/topics/", {"subject": self.subject.id, "name": "capitals"})
        self.assertEqual(r.status_code, 400)

    # --- question validation ---
    def test_create_question_with_tags_and_options(self):
        r = self.create()
        self.assertEqual(r.status_code, 201, r.content)
        self.assertEqual(r.data["tags"], ["europe", "geography"])
        self.assertEqual(len(r.data["options"]), 3)
        self.assertEqual(r.data["version"], 1)

    def test_requires_exactly_one_correct_option(self):
        opts = [{"text": "A", "is_correct": True}, {"text": "B", "is_correct": True}]
        self.assertEqual(self.create(options=opts).status_code, 400)
        opts = [{"text": "A", "is_correct": False}, {"text": "B", "is_correct": False}]
        self.assertEqual(self.create(options=opts).status_code, 400)

    def test_option_count_is_configurable_but_bounded(self):
        two = [{"text": "Yes", "is_correct": True}, {"text": "No", "is_correct": False}]
        self.assertEqual(self.create(options=two).status_code, 201)
        six = [{"text": f"o{i}", "is_correct": i == 0} for i in range(6)]
        self.assertEqual(self.create(text="Six options question here", options=six).status_code, 201)
        one = [{"text": "Only", "is_correct": True}]
        self.assertEqual(self.create(text="One option q", options=one).status_code, 400)

    def test_rejects_duplicate_option_text_and_bad_marks(self):
        dup = [{"text": "A", "is_correct": True}, {"text": "a", "is_correct": False}]
        self.assertEqual(self.create(options=dup).status_code, 400)
        self.assertEqual(self.create(marks="0").status_code, 400)
        self.assertEqual(self.create(negative_marks="-1").status_code, 400)

    def test_topic_must_belong_to_subject(self):
        other = Subject.objects.create(name="History")
        r = self.client.post("/api/questions/", payload(other, self.topic), format="json")
        self.assertEqual(r.status_code, 400)
        self.assertIn("topic", r.data)

    def test_archived_subject_cannot_receive_new_questions(self):
        self.subject.is_active = False
        self.subject.save()
        self.assertEqual(self.create().status_code, 400)

    # --- duplicates ---
    def test_exact_duplicate_blocked_unless_allowed(self):
        self.assertEqual(self.create().status_code, 201)
        r = self.create(text="  what is the CAPITAL of france ")
        self.assertEqual(r.status_code, 400)
        self.assertIn("text", r.data)
        self.assertEqual(self.create(text="what is the capital of france?", allow_duplicate=True).status_code, 201)

    def test_check_duplicates_finds_similar(self):
        self.create()
        r = self.client.post("/api/questions/check-duplicates/", {"text": "What is the capital of France"}, format="json")
        self.assertTrue(r.data[0]["exact"])
        r = self.client.post("/api/questions/check-duplicates/", {"text": "What is the capital city of France?"}, format="json")
        self.assertEqual(len(r.data), 1)
        self.assertFalse(r.data[0]["exact"])
        r = self.client.post("/api/questions/check-duplicates/", {"text": "Who wrote Hamlet in 1600?"}, format="json")
        self.assertEqual(r.data, [])

    # --- editing keeps option ids and versions ---
    def test_edit_keeps_option_ids_and_bumps_version_only_on_scoring_changes(self):
        qid = self.create().data["id"]
        data = self.client.get(f"/api/questions/{qid}/").data
        old_ids = [o["id"] for o in data["options"]]

        # Non-scoring edit: explanation only.
        r = self.client.patch(f"/api/questions/{qid}/", {"explanation": "New explanation"}, format="json")
        self.assertEqual(r.data["version"], 1)

        # Change correct answer, keep ids, drop one option, add one.
        opts = [
            {"id": old_ids[0], "text": "Paris", "is_correct": False},
            {"id": old_ids[1], "text": "Rome", "is_correct": True},
            {"text": "Madrid", "is_correct": False},
        ]
        r = self.client.patch(f"/api/questions/{qid}/", {"options": opts}, format="json")
        self.assertEqual(r.status_code, 200, r.content)
        new_ids = [o["id"] for o in r.data["options"]]
        self.assertEqual(new_ids[:2], old_ids[:2])
        self.assertEqual(r.data["version"], 2)
        self.assertFalse(QuestionOption.objects.filter(id=old_ids[2]).exists())

    def test_cannot_reference_another_questions_option(self):
        a = self.create().data
        b = self.create(text="A completely different question").data
        opts = [
            {"id": b["options"][0]["id"], "text": "Paris", "is_correct": True},
            {"text": "X", "is_correct": False},
        ]
        r = self.client.patch(f"/api/questions/{a['id']}/", {"options": opts}, format="json")
        self.assertEqual(r.status_code, 400)

    # --- list filters / search ---
    def test_filters_search_and_tags(self):
        self.create()
        self.create(text="Hard history question", difficulty="hard", tags=["history"], explanation="Dates matter.")
        self.assertEqual(self.client.get("/api/questions/?difficulty=hard").data["count"], 1)
        self.assertEqual(self.client.get("/api/questions/?search=capital").data["count"], 1)
        self.assertEqual(self.client.get("/api/questions/?tag=history").data["count"], 1)
        self.assertEqual(self.client.get(f"/api/questions/?subject={self.subject.id}&is_active=true").data["count"], 2)
        self.assertEqual(self.client.get("/api/tags/").data, ["europe", "geography", "history"])

    # --- duplicate action / delete / bulk ---
    def test_duplicate_creates_inactive_copy_with_options_and_tags(self):
        src = self.create().data
        r = self.client.post(f"/api/questions/{src['id']}/duplicate/")
        self.assertEqual(r.status_code, 201)
        self.assertFalse(r.data["is_active"])
        self.assertEqual(len(r.data["options"]), 3)
        self.assertEqual(r.data["tags"], src["tags"])
        self.assertNotEqual({o["id"] for o in r.data["options"]}, {o["id"] for o in src["options"]})

    def test_delete_archives(self):
        qid = self.create().data["id"]
        self.assertEqual(self.client.delete(f"/api/questions/{qid}/").status_code, 204)
        self.assertFalse(Question.objects.get(id=qid).is_active)

    def test_bulk_actions(self):
        a = self.create().data["id"]
        b = self.create(text="Second question text").data["id"]
        r = self.client.post("/api/questions/bulk/", {"ids": [a, b], "action": "deactivate"}, format="json")
        self.assertEqual(r.data["updated"], 2)
        self.assertEqual(Question.objects.filter(is_active=False).count(), 2)
        self.client.post("/api/questions/bulk/", {"ids": [a, b], "action": "set_difficulty", "difficulty": "hard"}, format="json")
        self.assertEqual(Question.objects.filter(difficulty="hard").count(), 2)
        self.client.post("/api/questions/bulk/", {"ids": [a], "action": "add_tags", "tags": ["Extra"]}, format="json")
        self.assertIn("extra", self.client.get(f"/api/questions/{a}/").data["tags"])
        self.assertEqual(
            self.client.post("/api/questions/bulk/", {"ids": [a], "action": "set_difficulty"}, format="json").status_code, 400
        )

    # --- image upload ---
    def test_image_upload_and_remove(self):
        from PIL import Image

        qid = self.create().data["id"]
        buf = io.BytesIO()
        Image.new("RGB", (4, 4), "red").save(buf, "PNG")
        f = SimpleUploadedFile("pic.png", buf.getvalue(), content_type="image/png")
        r = self.client.post(f"/api/questions/{qid}/image/", {"image": f}, format="multipart")
        self.assertEqual(r.status_code, 200, r.content)
        self.assertTrue(r.data["image_url"])
        bad = SimpleUploadedFile("x.png", b"not an image", content_type="image/png")
        self.assertEqual(self.client.post(f"/api/questions/{qid}/image/", {"image": bad}, format="multipart").status_code, 400)
        r = self.client.delete(f"/api/questions/{qid}/image/")
        self.assertIsNone(r.data["image_url"])
