from django.conf import settings
from django.db import models
from django.db.models.signals import post_delete
from django.dispatch import receiver

from core.ids import new_public_id
from core.media import unique_upload_path


class StudyNote(models.Model):
    """A study note or "capsule" (for example a month of current affairs) written by an admin and read by students."""

    public_id = models.CharField(max_length=24, unique=True, editable=False, default=new_public_id)
    title = models.CharField(max_length=200, blank=True)
    title_hi = models.CharField(max_length=200, blank=True)
    body = models.TextField(blank=True)
    body_hi = models.TextField(blank=True)
    category = models.ForeignKey("exams.ExamCategory", null=True, blank=True, on_delete=models.SET_NULL, related_name="notes")
    subject = models.ForeignKey("questionbank.Subject", null=True, blank=True, on_delete=models.SET_NULL, related_name="notes")
    # The period the note covers, for sorting ("October 2026 current affairs").
    note_date = models.DateField(null=True, blank=True, db_index=True)
    is_published = models.BooleanField(default=False, db_index=True)
    created_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, on_delete=models.SET_NULL, related_name="+")
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-note_date", "-created_at", "-id"]

    @property
    def display_title(self) -> str:
        return self.title or self.title_hi

    def __str__(self):
        return self.display_title


class TestSeries(models.Model):
    """A named, ordered group of tests, for example "SSC CGL 2026: 30 mocks", with the student's progress through it."""

    __test__ = False  # not a test case, whatever the name suggests

    public_id = models.CharField(max_length=24, unique=True, editable=False, default=new_public_id)
    title = models.CharField(max_length=200)
    description = models.TextField(blank=True)
    category = models.ForeignKey("exams.ExamCategory", null=True, blank=True, on_delete=models.SET_NULL, related_name="series")
    is_published = models.BooleanField(default=False, db_index=True)
    created_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, on_delete=models.SET_NULL, related_name="+")
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["title", "id"]
        verbose_name_plural = "test series"

    def __str__(self):
        return self.title


class SeriesItem(models.Model):
    series = models.ForeignKey(TestSeries, on_delete=models.CASCADE, related_name="items")
    exam = models.ForeignKey("exams.Exam", on_delete=models.CASCADE, related_name="series_items")
    order = models.PositiveIntegerField(default=0)

    class Meta:
        ordering = ["order", "id"]
        constraints = [models.UniqueConstraint(fields=["series", "exam"], name="one_exam_once_per_series")]


def popup_image_path(instance, filename):
    return unique_upload_path("popups", filename)


class Popup(models.Model):
    """
    A message that opens in a pop-up the next time a student visits (a new launch, a new feature, an important date). Each
    student sees it once, until they close it or press its button. It stops showing when `ends_at` passes or it is switched off.
    """

    class Audience(models.TextChoices):
        ALL = "all", "All students"
        EXAM = "exam", "Students preparing for an exam"
        BATCH = "batch", "A batch"

    title = models.CharField(max_length=120)
    body = models.TextField(blank=True, max_length=800)
    # An optional button: a page of this app ("/student/plan") or a web address ("https://...").
    link = models.CharField(max_length=300, blank=True)
    button_label = models.CharField(max_length=30, blank=True, default="Open")
    # An optional banner shown above the title; kept in the same storage as question pictures (Supabase in production).
    image = models.ImageField(upload_to=popup_image_path, null=True, blank=True)
    audience = models.CharField(max_length=5, choices=Audience.choices, default=Audience.ALL)
    category = models.ForeignKey("exams.ExamCategory", null=True, blank=True, on_delete=models.CASCADE, related_name="+")
    batch = models.ForeignKey("exams.Batch", null=True, blank=True, on_delete=models.CASCADE, related_name="+")
    starts_at = models.DateTimeField(null=True, blank=True)
    ends_at = models.DateTimeField(null=True, blank=True, db_index=True)
    is_active = models.BooleanField(default=True, db_index=True)
    created_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, on_delete=models.SET_NULL, related_name="+")
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-created_at", "-id"]


class PopupSeen(models.Model):
    """That a student has closed (or used) a pop-up, so it is not shown to them again."""

    popup = models.ForeignKey(Popup, on_delete=models.CASCADE, related_name="seen")
    student = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="+")
    seen_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        unique_together = [("popup", "student")]


@receiver(post_delete, sender=Popup)
def delete_popup_image(sender, instance, **kwargs):
    if instance.image:
        instance.image.storage.delete(instance.image.name)
