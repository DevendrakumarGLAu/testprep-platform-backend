from django.conf import settings
from django.db import models
from django.db.models import Q

from core.ids import new_public_id


class Attempt(models.Model):
    class Status(models.TextChoices):
        IN_PROGRESS = "in_progress", "In progress"
        SUBMITTED = "submitted", "Submitted"

    public_id = models.CharField(max_length=24, unique=True, editable=False, default=new_public_id)
    exam = models.ForeignKey("exams.Exam", on_delete=models.PROTECT, related_name="attempts")
    student = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="attempts")
    attempt_number = models.PositiveIntegerField()
    exam_version = models.PositiveIntegerField()
    status = models.CharField(max_length=12, choices=Status.choices, default=Status.IN_PROGRESS, db_index=True)

    # The server decides when the attempt started and when it ends. The browser clock is never trusted.
    started_at = models.DateTimeField(auto_now_add=True)
    deadline = models.DateTimeField()
    submitted_at = models.DateTimeField(null=True, blank=True)
    auto_submitted = models.BooleanField(default=False)

    # Rules frozen at start, so later exam edits cannot change this attempt.
    duration_minutes = models.PositiveIntegerField()
    total_marks = models.DecimalField(max_digits=10, decimal_places=2)
    passing_marks = models.DecimalField(max_digits=10, decimal_places=2)
    negative_marking_enabled = models.BooleanField()
    sectional_timing = models.BooleanField(default=False)
    unanswered_penalty = models.DecimalField(max_digits=6, decimal_places=2, default=0)

    # Filled in when the attempt is scored.
    score = models.DecimalField(max_digits=10, decimal_places=2, null=True, blank=True)
    correct_count = models.PositiveIntegerField(default=0)
    incorrect_count = models.PositiveIntegerField(default=0)
    unanswered_count = models.PositiveIntegerField(default=0)
    accuracy = models.DecimalField(max_digits=5, decimal_places=2, null=True, blank=True)
    passed = models.BooleanField(null=True, blank=True)
    time_taken_seconds = models.PositiveIntegerField(null=True, blank=True)

    class Meta:
        ordering = ["-started_at", "-id"]
        constraints = [
            models.UniqueConstraint(fields=["exam", "student", "attempt_number"], name="uniq_attempt_number"),
            # At most one running attempt per student per test.
            models.UniqueConstraint(
                fields=["exam", "student"], condition=Q(status="in_progress"), name="one_active_attempt"
            ),
        ]


class AttemptQuestion(models.Model):
    """A question exactly as this student received it (content, marks, option order, correct answer)."""

    attempt = models.ForeignKey(Attempt, on_delete=models.CASCADE, related_name="questions")
    question = models.ForeignKey("questionbank.Question", null=True, on_delete=models.SET_NULL, related_name="+")
    section_name = models.CharField(max_length=120)
    section_order = models.PositiveIntegerField(default=0)
    order = models.PositiveIntegerField()  # position within the whole attempt, from 0

    subject_name = models.CharField(max_length=120, blank=True)
    text = models.TextField(blank=True)
    text_hi = models.TextField(blank=True)
    image_path = models.CharField(max_length=255, blank=True)
    options = models.JSONField()  # [{"id": 1, "text": "...", "is_correct": bool}] in the order shown
    explanation = models.TextField(blank=True)
    explanation_hi = models.TextField(blank=True)
    video_url = models.URLField(max_length=300, blank=True)
    marks = models.DecimalField(max_digits=6, decimal_places=2)
    negative_marks = models.DecimalField(max_digits=8, decimal_places=4, default=0)

    selected_option_id = models.IntegerField(null=True, blank=True)
    # Seconds the student spent on this question. Sent by the exam screen as a running total and validated by the server.
    time_spent_seconds = models.PositiveIntegerField(default=0)
    marked_for_review = models.BooleanField(default=False)
    visited = models.BooleanField(default=False)
    answered_at = models.DateTimeField(null=True, blank=True)

    is_correct = models.BooleanField(null=True, blank=True)
    marks_awarded = models.DecimalField(max_digits=9, decimal_places=4, null=True, blank=True)

    class Meta:
        ordering = ["order"]
        constraints = [models.UniqueConstraint(fields=["attempt", "order"], name="uniq_attempt_question_order")]


class AttemptSection(models.Model):
    """The clock of one section in a test with sectional timing. Sections run strictly one after another."""

    class ClosedBy(models.TextChoices):
        OPEN = "", "Open"
        SUBMITTED = "submitted", "Submitted by the student"
        EXPIRED = "expired", "Time ran out"

    attempt = models.ForeignKey(Attempt, on_delete=models.CASCADE, related_name="section_states")
    name = models.CharField(max_length=120)
    order = models.PositiveIntegerField()  # matches AttemptQuestion.section_order
    duration_seconds = models.PositiveIntegerField()
    started_at = models.DateTimeField(null=True, blank=True)
    ended_at = models.DateTimeField(null=True, blank=True)
    closed_by = models.CharField(max_length=10, choices=ClosedBy.choices, blank=True, default="")

    class Meta:
        ordering = ["order"]
        constraints = [models.UniqueConstraint(fields=["attempt", "order"], name="uniq_attempt_section")]


class SavedQuestion(models.Model):
    """A question a student bookmarked from a result review, with a private note. It is a copy of what they saw, answer included."""

    student = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="saved_questions")
    attempt_question = models.ForeignKey(AttemptQuestion, null=True, blank=True, on_delete=models.SET_NULL, related_name="saved_by")
    question = models.ForeignKey("questionbank.Question", null=True, blank=True, on_delete=models.SET_NULL, related_name="+")
    exam_title = models.CharField(max_length=200, blank=True)
    subject_name = models.CharField(max_length=120, blank=True)
    text = models.TextField(blank=True)
    text_hi = models.TextField(blank=True)
    options = models.JSONField(default=list)  # [{"id", "text", "text_hi", "is_correct"}]
    selected_option_id = models.IntegerField(null=True, blank=True)
    explanation = models.TextField(blank=True)
    explanation_hi = models.TextField(blank=True)
    note = models.TextField(blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-created_at", "-id"]
        constraints = [
            models.UniqueConstraint(fields=["student", "attempt_question"], condition=models.Q(attempt_question__isnull=False), name="one_save_per_attempt_question"),
        ]
