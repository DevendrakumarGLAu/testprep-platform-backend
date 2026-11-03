from django.conf import settings
from django.db import models


class StudyPlan(models.Model):
    """A student's plan for one exam date: a day-by-day list of tests and revision. A student has one plan at a time."""

    student = models.OneToOneField(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="study_plan")
    target_name = models.CharField(max_length=120)  # what they are preparing for, in their own words
    category = models.ForeignKey("exams.ExamCategory", null=True, blank=True, on_delete=models.SET_NULL, related_name="+")
    exam_date = models.DateField()
    daily_minutes = models.PositiveSmallIntegerField(default=60)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)


class StudyTask(models.Model):
    class Kind(models.TextChoices):
        TEST = "test", "Take a test"
        REVISE = "revise", "Revise your mistakes"
        PRACTICE = "practice", "Practise weak topics"
        LISTEN = "listen", "Listen to explanations"
        EXAM_DAY = "exam_day", "Exam day"

    plan = models.ForeignKey(StudyPlan, on_delete=models.CASCADE, related_name="tasks")
    date = models.DateField(db_index=True)
    kind = models.CharField(max_length=10, choices=Kind.choices)
    title = models.CharField(max_length=200)
    minutes = models.PositiveSmallIntegerField(default=15)
    exam = models.ForeignKey("exams.Exam", null=True, blank=True, on_delete=models.SET_NULL, related_name="+")
    # The subject this task is about, when the plan is organised by subject.
    subject = models.ForeignKey("questionbank.Subject", null=True, blank=True, on_delete=models.SET_NULL, related_name="+")
    done = models.BooleanField(default=False)
    done_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["date", "id"]


class Feedback(models.Model):
    """A note a signed-in person sent from the feedback button: something broken, an idea, or anything else."""

    class Kind(models.TextChoices):
        BUG = "bug", "Something is broken"
        IDEA = "idea", "An idea"
        OTHER = "other", "Something else"

    class Status(models.TextChoices):
        NEW = "new", "New"
        DONE = "done", "Handled"

    user = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, on_delete=models.SET_NULL, related_name="feedback")
    kind = models.CharField(max_length=6, choices=Kind.choices, default=Kind.OTHER)
    message = models.TextField()
    page = models.CharField(max_length=300, blank=True)  # where in the app they were
    device = models.CharField(max_length=300, blank=True)  # their browser, to help find a problem
    status = models.CharField(max_length=4, choices=Status.choices, default=Status.NEW, db_index=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-created_at", "-id"]


class Doubt(models.Model):
    """A question a student asked about one question of a test, answered by staff (with or without AI help)."""

    class Status(models.TextChoices):
        OPEN = "open", "Waiting for an answer"
        ANSWERED = "answered", "Answered"
        RESOLVED = "resolved", "Resolved"

    student = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="doubts")
    question = models.ForeignKey("questionbank.Question", null=True, blank=True, on_delete=models.SET_NULL, related_name="+")
    attempt_question = models.ForeignKey("attempts.AttemptQuestion", null=True, blank=True, on_delete=models.SET_NULL, related_name="+")
    exam_title = models.CharField(max_length=200, blank=True)
    # A copy of what the student saw, so the thread still makes sense if the question is edited or removed later.
    question_text = models.TextField(blank=True)
    question_text_hi = models.TextField(blank=True)
    options = models.JSONField(default=list)  # [{"text", "text_hi", "is_correct"}]
    explanation = models.TextField(blank=True)
    explanation_hi = models.TextField(blank=True)
    selected_option_text = models.CharField(max_length=500, blank=True)
    status = models.CharField(max_length=10, choices=Status.choices, default=Status.OPEN, db_index=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-updated_at", "-id"]


class DoubtReply(models.Model):
    class Role(models.TextChoices):
        STUDENT = "student", "Student"
        STAFF = "staff", "Teacher"

    doubt = models.ForeignKey(Doubt, on_delete=models.CASCADE, related_name="replies")
    author = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, on_delete=models.SET_NULL, related_name="+")
    role = models.CharField(max_length=8, choices=Role.choices)
    body = models.TextField()
    via_ai = models.BooleanField(default=False)  # a teacher used the AI suggestion for this reply
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["created_at", "id"]
