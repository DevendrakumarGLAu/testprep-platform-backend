from django.conf import settings
from django.db import models


class Notification(models.Model):
    """A message for one person, shown under the bell in the top bar."""

    class Kind(models.TextChoices):
        ANNOUNCEMENT = "announcement", "Announcement"
        NEW_TEST = "new_test", "New test"
        RESULT = "result", "Result released"
        REMINDER = "reminder", "Reminder"
        REPORT = "report", "Question report"
        WEEKLY = "weekly", "Weekly summary"

    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="notifications")
    kind = models.CharField(max_length=14, choices=Kind.choices, default=Kind.ANNOUNCEMENT)
    title = models.CharField(max_length=200)
    body = models.TextField(blank=True)
    link = models.CharField(max_length=200, blank=True)  # an app route such as /student/tests/abc
    # Stops the same message being sent twice to one person, for example "start:12" for the reminder of test 12.
    ref = models.CharField(max_length=80, blank=True)
    created_at = models.DateTimeField(auto_now_add=True, db_index=True)
    read_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["-created_at", "-id"]
        indexes = [models.Index(fields=["user", "read_at"])]
        constraints = [
            models.UniqueConstraint(fields=["user", "ref"], condition=~models.Q(ref=""), name="one_notification_per_ref"),
        ]
