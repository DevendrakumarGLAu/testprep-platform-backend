from django.conf import settings
from django.db import models


class ImportJob(models.Model):
    class Source(models.TextChoices):
        CSV = "csv", "CSV"
        EXCEL = "excel", "Excel"
        PDF = "pdf", "PDF"
        WEB = "web", "Website"  # reserved: extraction is a future integration point

    class Status(models.TextChoices):
        UPLOADED = "uploaded", "Uploaded"
        PROCESSED = "processed", "Ready to review"
        IMPORTED = "imported", "Imported"

    created_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, on_delete=models.SET_NULL, related_name="+")
    source_type = models.CharField(max_length=10, choices=Source.choices)
    filename = models.CharField(max_length=255)
    status = models.CharField(max_length=10, choices=Status.choices, default=Status.UPLOADED, db_index=True)

    headers = models.JSONField(default=list)      # column names found in the file (canonical names for PDF)
    mapping = models.JSONField(default=dict)      # field -> column name(s)
    defaults = models.JSONField(default=dict)     # subject / topic / difficulty / marks used when a row has none
    create_missing = models.BooleanField(default=False)
    warnings = models.JSONField(default=list)

    total_rows = models.PositiveIntegerField(default=0)
    valid_rows = models.PositiveIntegerField(default=0)
    invalid_rows = models.PositiveIntegerField(default=0)
    duplicate_rows = models.PositiveIntegerField(default=0)
    imported_count = models.PositiveIntegerField(default=0)
    failed_count = models.PositiveIntegerField(default=0)
    skipped_count = models.PositiveIntegerField(default=0)

    created_at = models.DateTimeField(auto_now_add=True)
    completed_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["-created_at", "-id"]


class ImportRow(models.Model):
    class State(models.TextChoices):
        PENDING = "pending", "Pending"
        VALID = "valid", "Valid"
        INVALID = "invalid", "Invalid"
        DUPLICATE = "duplicate", "Possible duplicate"

    class Result(models.TextChoices):
        PENDING = "pending", "Pending"
        IMPORTED = "imported", "Imported"
        SKIPPED = "skipped", "Skipped"
        FAILED = "failed", "Failed"

    job = models.ForeignKey(ImportJob, on_delete=models.CASCADE, related_name="rows")
    row_number = models.PositiveIntegerField()  # as the user sees it: header is row 1 for tables
    raw = models.JSONField()
    normalized = models.JSONField(null=True, blank=True)
    status = models.CharField(max_length=10, choices=State.choices, default=State.PENDING, db_index=True)
    errors = models.JSONField(default=list)
    duplicate_of = models.ForeignKey("questionbank.Question", null=True, blank=True, on_delete=models.SET_NULL, related_name="+")
    duplicate_info = models.JSONField(null=True, blank=True)
    selected = models.BooleanField(default=False)
    result = models.CharField(max_length=10, choices=Result.choices, default=Result.PENDING)
    result_message = models.CharField(max_length=300, blank=True)
    question = models.ForeignKey("questionbank.Question", null=True, blank=True, on_delete=models.SET_NULL, related_name="+")

    class Meta:
        ordering = ["row_number"]
        constraints = [models.UniqueConstraint(fields=["job", "row_number"], name="uniq_import_row")]
