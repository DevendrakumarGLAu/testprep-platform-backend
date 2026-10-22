import hashlib
import re

from django.conf import settings
from django.db import models


def normalize_text(text: str) -> str:
    """Lower-cased, punctuation-free, whitespace-collapsed form used for duplicate detection."""
    text = re.sub(r"[^\w\s]", " ", (text or "").lower())
    return re.sub(r"\s+", " ", text).strip()


def text_hash(text: str) -> str:
    return hashlib.sha1(normalize_text(text).encode("utf-8")).hexdigest()


class Subject(models.Model):
    name = models.CharField(max_length=120, unique=True)
    description = models.TextField(blank=True)
    is_active = models.BooleanField(default=True)  # inactive == archived
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["name"]

    def __str__(self):
        return self.name


class Topic(models.Model):
    subject = models.ForeignKey(Subject, on_delete=models.PROTECT, related_name="topics")
    name = models.CharField(max_length=120)
    description = models.TextField(blank=True)
    is_active = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["subject__name", "name"]
        constraints = [models.UniqueConstraint(fields=["subject", "name"], name="uniq_topic_per_subject")]

    def __str__(self):
        return f"{self.subject.name} / {self.name}"


class Tag(models.Model):
    name = models.CharField(max_length=40, unique=True)

    class Meta:
        ordering = ["name"]

    def __str__(self):
        return self.name


class Difficulty(models.TextChoices):
    EASY = "easy", "Easy"
    MEDIUM = "medium", "Medium"
    HARD = "hard", "Hard"


class QuestionType(models.TextChoices):
    SINGLE_MCQ = "single_mcq", "Single-correct MCQ"


class Source(models.TextChoices):
    MANUAL = "manual", "Manual"
    CSV = "csv", "CSV import"
    EXCEL = "excel", "Excel import"
    PDF = "pdf", "PDF import"
    WEB = "web", "Website extraction"


class Question(models.Model):
    # English is `text`; Hindi is `text_hi`. At least one language must be complete (checked by the serializer).
    text = models.TextField(blank=True)
    text_hi = models.TextField(blank=True)
    text_hash = models.CharField(max_length=40, db_index=True, editable=False)
    subject = models.ForeignKey(Subject, on_delete=models.PROTECT, related_name="questions")
    topic = models.ForeignKey(Topic, null=True, blank=True, on_delete=models.PROTECT, related_name="questions")
    difficulty = models.CharField(max_length=10, choices=Difficulty.choices, default=Difficulty.MEDIUM, db_index=True)
    question_type = models.CharField(max_length=20, choices=QuestionType.choices, default=QuestionType.SINGLE_MCQ)
    explanation = models.TextField(blank=True)
    explanation_hi = models.TextField(blank=True)
    marks = models.DecimalField(max_digits=6, decimal_places=2, default=1)
    negative_marks = models.DecimalField(max_digits=6, decimal_places=2, default=0)  # stored as a positive deduction
    tags = models.ManyToManyField(Tag, blank=True, related_name="questions")
    image = models.ImageField(upload_to="questions/", null=True, blank=True)
    is_active = models.BooleanField(default=True, db_index=True)
    source = models.CharField(max_length=10, choices=Source.choices, default=Source.MANUAL)
    source_info = models.CharField(max_length=255, blank=True)
    # Bumped whenever scoring-relevant content changes, so attempts can tell which version they saw.
    version = models.PositiveIntegerField(default=1)
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, on_delete=models.SET_NULL, related_name="+"
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-created_at", "-id"]

    @property
    def display_text(self) -> str:
        return self.text or self.text_hi

    def save(self, *args, **kwargs):
        self.text_hash = text_hash(self.display_text)
        super().save(*args, **kwargs)

    def __str__(self):
        return self.display_text[:60]


class QuestionOption(models.Model):
    question = models.ForeignKey(Question, on_delete=models.CASCADE, related_name="options")
    text = models.TextField(blank=True)
    text_hi = models.TextField(blank=True)
    order = models.PositiveIntegerField(default=0)
    is_correct = models.BooleanField(default=False)

    class Meta:
        ordering = ["order", "id"]
