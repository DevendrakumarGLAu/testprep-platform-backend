from django.conf import settings
from django.db import models

from core.ids import new_public_id


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
