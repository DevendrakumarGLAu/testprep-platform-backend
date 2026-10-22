from django.contrib.auth.models import AbstractUser
from django.db import models


class Role(models.TextChoices):
    SUPER_ADMIN = "super_admin", "Super Admin"
    ADMIN = "admin", "Admin"
    STUDENT = "student", "Student"


class User(AbstractUser):
    email = models.EmailField(unique=True)
    role = models.CharField(max_length=20, choices=Role.choices, default=Role.STUDENT, db_index=True)
    phone = models.CharField(max_length=20, blank=True)
    created_by = models.ForeignKey(
        "self", null=True, blank=True, on_delete=models.SET_NULL, related_name="created_users"
    )

    REQUIRED_FIELDS = ["email"]

    @property
    def is_super_admin(self):
        return self.role == Role.SUPER_ADMIN

    @property
    def is_admin_role(self):
        return self.role in (Role.SUPER_ADMIN, Role.ADMIN)

    def save(self, *args, **kwargs):
        if self.email:
            self.email = self.email.strip().lower()
        if self.role == Role.SUPER_ADMIN:
            self.is_staff = True
        super().save(*args, **kwargs)


class ActivityLog(models.Model):
    """Audit trail of administrative actions."""

    actor = models.ForeignKey(User, null=True, on_delete=models.SET_NULL, related_name="activity")
    action = models.CharField(max_length=60)
    target_type = models.CharField(max_length=60, blank=True)
    target_id = models.CharField(max_length=40, blank=True)
    detail = models.TextField(blank=True)
    created_at = models.DateTimeField(auto_now_add=True, db_index=True)

    class Meta:
        ordering = ["-created_at"]


class PlatformSettings(models.Model):
    """One row of platform-wide configuration, managed by the Super Admin."""

    platform_name = models.CharField(max_length=60, default="TestPrep")
    allow_registration = models.BooleanField(default=True)
    support_email = models.EmailField(blank=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name_plural = "platform settings"

    def save(self, *args, **kwargs):
        self.pk = 1  # singleton
        super().save(*args, **kwargs)

    @classmethod
    def load(cls):
        obj, _ = cls.objects.get_or_create(pk=1)
        return obj


def log_activity(actor, action, target=None, detail=""):
    ActivityLog.objects.create(
        actor=actor if getattr(actor, "pk", None) else None,
        action=action,
        target_type=target.__class__.__name__ if target is not None else "",
        target_id=str(getattr(target, "pk", "")) if target is not None else "",
        detail=detail,
    )
