import io
from unittest.mock import MagicMock, patch
from urllib.error import HTTPError

from django.core.files.storage import InMemoryStorage, default_storage
from django.core.files.uploadedfile import SimpleUploadedFile
from django.core.management import call_command
from django.core.management.base import CommandError
from django.test import SimpleTestCase, override_settings
from PIL import Image
from rest_framework.test import APITestCase

from accounts.models import Role, User
from attempts.models import AttemptQuestion
from attempts.tests_prep import PrepBase
from config.storage import build_storages, public_base
from core.media import MAX_IMAGE_SIDE, media_url, prepare_image, unique_upload_path
from questionbank.models import Question, QuestionOption, Subject


class FakeCloud(InMemoryStorage):
    """Stands in for Supabase: keeps files in memory and hands out full https addresses, as a remote storage does."""

    def url(self, name):
        return f"https://cdn.example/storage/{name}"


CLOUD = {"default": {"BACKEND": "core.tests_media.FakeCloud"}, "staticfiles": {"BACKEND": "django.contrib.staticfiles.storage.StaticFilesStorage"}}


def picture(size=(40, 30), fmt="PNG", name="pic"):
    buf = io.BytesIO()
    Image.new("RGB", size, (200, 30, 30)).save(buf, fmt)
    ext = {"PNG": "png", "JPEG": "jpg", "WEBP": "webp", "GIF": "gif", "BMP": "bmp"}[fmt]
    return SimpleUploadedFile(f"{name}.{ext}", buf.getvalue(), content_type=f"image/{ext}")


class StorageSettingsTests(SimpleTestCase):
    ENV = {
        "SUPABASE_S3_ENDPOINT": "https://abcd.supabase.co/storage/v1/s3",
        "SUPABASE_S3_ACCESS_KEY_ID": "key-id", "SUPABASE_S3_SECRET_ACCESS_KEY": "secret", "SUPABASE_S3_REGION": "ap-south-1",
    }

    def test_without_supabase_settings_files_stay_on_this_machine(self):
        s = build_storages({})
        self.assertEqual(s["default"]["BACKEND"], "django.core.files.storage.FileSystemStorage")
        self.assertEqual(build_storages({"SUPABASE_S3_ENDPOINT": "   "})["default"]["BACKEND"], "django.core.files.storage.FileSystemStorage")

    def test_static_files_are_never_moved(self):
        self.assertIn("StaticFilesStorage", build_storages({})["staticfiles"]["BACKEND"])
        self.assertIn("StaticFilesStorage", build_storages(self.ENV)["staticfiles"]["BACKEND"])

    def test_with_supabase_settings_files_go_to_the_bucket(self):
        d = build_storages(self.ENV)["default"]
        o = d["OPTIONS"]
        self.assertEqual(d["BACKEND"], "storages.backends.s3.S3Storage")
        self.assertEqual((o["bucket_name"], o["endpoint_url"], o["region_name"]), ("question-images", self.ENV["SUPABASE_S3_ENDPOINT"], "ap-south-1"))
        self.assertEqual((o["access_key"], o["secret_key"]), ("key-id", "secret"))
        self.assertEqual(o["addressing_style"], "path")
        self.assertFalse(o["querystring_auth"])
        self.assertFalse(o["file_overwrite"])
        self.assertIsNone(o["default_acl"])
        self.assertIn("immutable", o["object_parameters"]["CacheControl"])

    def test_the_bucket_name_can_be_chosen(self):
        o = build_storages({**self.ENV, "SUPABASE_BUCKET": "my-pics"})["default"]["OPTIONS"]
        self.assertEqual(o["bucket_name"], "my-pics")
        self.assertTrue(o["custom_domain"].endswith("/object/public/my-pics"))

    def test_the_public_address_is_worked_out_from_the_endpoint(self):
        self.assertEqual(
            public_base("https://abcd.supabase.co/storage/v1/s3", "question-images"),
            "abcd.supabase.co/storage/v1/object/public/question-images",
        )
        self.assertEqual(public_base("https://abcd.supabase.co/storage/v1/s3/", "b"), "abcd.supabase.co/storage/v1/object/public/b")

    def test_the_public_address_can_be_overridden(self):
        self.assertEqual(public_base("https://abcd.supabase.co/storage/v1/s3", "b", "https://img.myschool.in/pics/"), "img.myschool.in/pics")
        o = build_storages({**self.ENV, "SUPABASE_PUBLIC_BASE": "https://img.myschool.in/pics"})["default"]["OPTIONS"]
        self.assertEqual(o["custom_domain"], "img.myschool.in/pics")

    def test_the_s3_backend_can_really_be_built_from_these_options(self):
        from storages.backends.s3 import S3Storage

        o = dict(build_storages(self.ENV)["default"]["OPTIONS"])
        s = S3Storage(**o)
        self.assertEqual(s.url("questions/abc.png"), "https://abcd.supabase.co/storage/v1/object/public/question-images/questions/abc.png")


class UploadNameTests(SimpleTestCase):
    def test_every_name_is_new_and_keeps_a_picture_extension(self):
        a, b = unique_upload_path("questions", "Diagram 1.PNG"), unique_upload_path("questions", "Diagram 1.PNG")
        self.assertNotEqual(a, b)
        self.assertTrue(a.startswith("questions/") and a.endswith(".png"))
        self.assertTrue(unique_upload_path("exams", "photo.JPG").endswith(".jpg"))

    def test_a_file_without_an_extension_still_gets_one(self):
        self.assertTrue(unique_upload_path("questions", "noext").endswith(".png"))


@override_settings(STORAGES=CLOUD)
class MediaUrlTests(SimpleTestCase):
    def test_remote_storage_gives_a_full_address_untouched(self):
        self.assertEqual(media_url(MagicMock(), "questions/a.png"), "https://cdn.example/storage/questions/a.png")

    def test_nothing_gives_nothing(self):
        self.assertIsNone(media_url(MagicMock(), ""))
        self.assertIsNone(media_url(MagicMock(), None))

    def test_local_storage_is_made_absolute_from_the_request(self):
        with override_settings(STORAGES={"default": {"BACKEND": "django.core.files.storage.FileSystemStorage"}, "staticfiles": CLOUD["staticfiles"]}):
            req = MagicMock()
            req.build_absolute_uri.side_effect = lambda p: "http://testserver" + p
            self.assertEqual(media_url(req, "questions/a.png"), "http://testserver/media/questions/a.png")


class PrepareImageTests(SimpleTestCase):
    def test_a_normal_picture_is_stored_exactly_as_uploaded(self):
        f = picture((300, 200))
        original = f.read()
        f.seek(0)
        out = prepare_image(f)
        self.assertEqual(out.read(), original)

    def test_a_very_large_picture_is_shrunk_to_fit(self):
        out = prepare_image(picture((4000, 2000), "PNG"))
        img = Image.open(io.BytesIO(out.read()))
        self.assertEqual(max(img.size), MAX_IMAGE_SIDE)
        self.assertEqual(img.size, (1600, 800))  # same proportions

    def test_a_large_jpeg_stays_a_jpeg_and_a_large_webp_a_webp(self):
        self.assertEqual(Image.open(io.BytesIO(prepare_image(picture((3000, 3000), "JPEG")).read())).format, "JPEG")
        self.assertEqual(Image.open(io.BytesIO(prepare_image(picture((2500, 1000), "WEBP")).read())).format, "WEBP")

    def test_a_gif_is_left_alone_even_when_large(self):
        f = picture((2000, 2000), "GIF")
        original = f.read()
        f.seek(0)
        self.assertEqual(prepare_image(f).read(), original)

    def test_a_file_that_only_claims_to_be_a_picture_is_refused(self):
        from rest_framework.exceptions import ValidationError

        with self.assertRaises(ValidationError):
            prepare_image(SimpleUploadedFile("fake.png", b"this is not a picture", content_type="image/png"))

    def test_other_picture_formats_are_refused(self):
        from rest_framework.exceptions import ValidationError

        with self.assertRaises(ValidationError) as cm:
            prepare_image(picture((50, 50), "BMP"))
        self.assertIn("JPG, PNG, WebP or GIF", str(cm.exception.detail))


@override_settings(STORAGES=CLOUD)
class QuestionImageUploadTests(APITestCase):
    def setUp(self):
        self.admin = User.objects.create_user(username="adm", email="adm@x.com", password="Str0ng!pass99", role=Role.ADMIN)
        self.student = User.objects.create_user(username="stu", email="stu@x.com", password="Str0ng!pass99", role=Role.STUDENT)
        subject = Subject.objects.create(name="Maths")
        self.q = Question.objects.create(text="Find the angle in the figure", subject=subject)
        QuestionOption.objects.create(question=self.q, text="a", is_correct=True, order=0)
        QuestionOption.objects.create(question=self.q, text="b", is_correct=False, order=1)
        self.client.force_authenticate(self.admin)

    def upload(self, f=None, q=None):
        return self.client.post(f"/api/questions/{(q or self.q).id}/image/", {"image": f or picture()}, format="multipart")

    def test_an_uploaded_picture_is_stored_under_a_random_name_and_given_a_full_address(self):
        r = self.upload()
        self.assertEqual(r.status_code, 200, r.content)
        self.q.refresh_from_db()
        self.assertTrue(self.q.image.name.startswith("questions/") and self.q.image.name.endswith(".png"))
        self.assertEqual(r.data["image_url"], f"https://cdn.example/storage/{self.q.image.name}")
        self.assertTrue(default_storage.exists(self.q.image.name))

    def test_two_uploads_with_the_same_file_name_never_collide(self):
        other = Question.objects.create(text="Another figure question", subject=self.q.subject)
        self.upload(picture(name="figure"))
        self.upload(picture(name="figure"), q=other)
        self.q.refresh_from_db()
        other.refresh_from_db()
        self.assertNotEqual(self.q.image.name, other.image.name)

    def test_replacing_the_picture_removes_the_old_file_when_nothing_uses_it(self):
        self.upload()
        self.q.refresh_from_db()
        old = self.q.image.name
        self.upload(picture((60, 60)))
        self.q.refresh_from_db()
        self.assertNotEqual(self.q.image.name, old)
        self.assertFalse(default_storage.exists(old))
        self.assertTrue(default_storage.exists(self.q.image.name))

    def test_the_old_file_is_kept_when_a_students_past_attempt_still_shows_it(self):
        self.upload()
        self.q.refresh_from_db()
        old = self.q.image.name
        exam_attempt = self.make_attempt_row(old)
        self.upload(picture((60, 60)))
        self.assertTrue(default_storage.exists(old))
        self.assertTrue(AttemptQuestion.objects.filter(pk=exam_attempt.pk, image_path=old).exists())

    def make_attempt_row(self, path):
        from datetime import timedelta

        from django.utils import timezone

        from attempts.models import Attempt
        from exams.models import Exam

        exam = Exam.objects.create(title="E", duration_minutes=10, status="published")
        a = Attempt.objects.create(
            exam=exam, student=self.student, attempt_number=1, exam_version=1, deadline=timezone.now() + timedelta(minutes=5),
            duration_minutes=10, total_marks=1, passing_marks=0, negative_marking_enabled=False,
        )
        return AttemptQuestion.objects.create(attempt=a, question=self.q, section_name="S", order=0, options=[], marks=1, image_path=path)

    def test_the_file_is_kept_when_a_duplicated_question_shares_it(self):
        self.upload()
        self.q.refresh_from_db()
        twin = Question.objects.create(text="Same figure, other wording", subject=self.q.subject, image=self.q.image.name)
        r = self.client.delete(f"/api/questions/{self.q.id}/image/")
        self.assertEqual(r.status_code, 200)
        self.assertIsNone(r.data["image_url"])
        self.assertTrue(default_storage.exists(twin.image.name))

    def test_removing_the_picture_deletes_the_file_when_nothing_else_uses_it(self):
        self.upload()
        self.q.refresh_from_db()
        name = self.q.image.name
        r = self.client.delete(f"/api/questions/{self.q.id}/image/")
        self.assertIsNone(r.data["image_url"])
        self.assertFalse(default_storage.exists(name))
        self.q.refresh_from_db()
        self.assertFalse(self.q.image)

    def test_a_large_upload_is_shrunk_before_it_is_stored(self):
        r = self.upload(picture((3200, 1600)))
        self.assertEqual(r.status_code, 200)
        self.q.refresh_from_db()
        with default_storage.open(self.q.image.name) as f:
            self.assertEqual(Image.open(f).size, (1600, 800))

    def test_bad_uploads_are_refused_and_change_nothing(self):
        self.upload()
        self.q.refresh_from_db()
        keep = self.q.image.name
        too_big = SimpleUploadedFile("big.png", b"x" * (2 * 1024 * 1024 + 1), content_type="image/png")
        for bad in (too_big, SimpleUploadedFile("a.txt", b"hello", content_type="text/plain"), SimpleUploadedFile("fake.png", b"nope", content_type="image/png")):
            self.assertEqual(self.upload(bad).status_code, 400, bad.name)
        self.assertEqual(self.client.post(f"/api/questions/{self.q.id}/image/", {}, format="multipart").status_code, 400)
        self.q.refresh_from_db()
        self.assertEqual(self.q.image.name, keep)
        self.assertTrue(default_storage.exists(keep))

    def test_students_cannot_upload(self):
        self.client.force_authenticate(self.student)
        self.assertEqual(self.upload().status_code, 403)


@override_settings(STORAGES=CLOUD)
class StudentSeesTheImageTests(PrepBase):
    def setUp(self):
        super().setUp()
        self.login()

    def start_with_picture(self):
        exam = self.make_test("Figure test", self.ssc)
        Question.objects.filter(exam_items__section__exam=exam).update(image="questions/diagram.png")
        aid = self.client.post(f"/api/student/exams/{exam.public_id}/start/").data["attempt_id"]
        return exam, aid

    def test_the_question_picture_is_shown_from_the_storage_address(self):
        _, aid = self.start_with_picture()
        d = self.client.get(f"/api/student/attempts/{aid}/").data
        q = d["sections"][0]["questions"][0]
        self.assertEqual(q["image_url"], "https://cdn.example/storage/questions/diagram.png")

    def test_the_result_review_shows_it_too(self):
        _, aid = self.start_with_picture()
        self.client.post(f"/api/student/attempts/{aid}/submit/")
        d = self.client.get(f"/api/student/attempts/{aid}/result/").data
        self.assertEqual(d["questions"][0]["image_url"], "https://cdn.example/storage/questions/diagram.png")

    def test_a_question_without_a_picture_has_none(self):
        exam = self.make_test("Plain test", self.ssc)
        aid = self.client.post(f"/api/student/exams/{exam.public_id}/start/").data["attempt_id"]
        q = self.client.get(f"/api/student/attempts/{aid}/").data["sections"][0]["questions"][0]
        self.assertIsNone(q["image_url"])

    def test_the_attempt_keeps_the_picture_it_was_shown_after_the_question_changes(self):
        _, aid = self.start_with_picture()
        Question.objects.update(image="questions/new-diagram.png")
        q = self.client.get(f"/api/student/attempts/{aid}/").data["sections"][0]["questions"][0]
        self.assertEqual(q["image_url"], "https://cdn.example/storage/questions/diagram.png")


class CheckStorageCommandTests(SimpleTestCase):
    def run_it(self):
        out = io.StringIO()
        call_command("check_storage", stdout=out)
        return out.getvalue()

    def test_local_storage_passes_and_says_where_pictures_are_kept(self):
        text = self.run_it()
        self.assertIn("local media folder", text)
        self.assertIn("Storage works.", text)

    @override_settings(STORAGES=CLOUD)
    def test_remote_storage_is_opened_like_a_browser_and_cleaned_up(self):
        response = MagicMock(status=200)
        response.headers = {"Content-Type": "image/png"}
        response.__enter__.return_value = response
        with patch("urllib.request.urlopen", return_value=response) as opened:
            text = self.run_it()
        self.assertIn("Opened like a browser: ok", text)
        self.assertIn("Removed the test file.", text)
        self.assertTrue(opened.call_args[0][0].startswith("https://cdn.example/storage/healthcheck/"))
        self.assertEqual(list(default_storage.listdir("healthcheck")[1]), [])

    @override_settings(STORAGES=CLOUD)
    def test_a_bucket_that_is_not_public_is_explained(self):
        err = HTTPError("https://cdn.example/x", 404, "Not Found", {}, None)
        with patch("urllib.request.urlopen", side_effect=err):
            with self.assertRaises(CommandError) as cm:
                self.run_it()
        self.assertIn("Make the bucket public", str(cm.exception))
        self.assertEqual(list(default_storage.listdir("healthcheck")[1]), [])  # still cleaned up

    @override_settings(STORAGES=CLOUD)
    def test_a_storage_that_cannot_be_written_is_reported(self):
        with patch.object(FakeCloud, "_save", side_effect=PermissionError("bad keys")):
            with self.assertRaises(CommandError) as cm:
                self.run_it()
        self.assertIn("Could not save to the storage", str(cm.exception))
        self.assertIn("bad keys", str(cm.exception))
