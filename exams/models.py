from decimal import Decimal

from django.conf import settings
from django.core.validators import MaxValueValidator, MinValueValidator
from django.db import models
from django.db.models import Count, IntegerField, OuterRef, Q, Subquery, Sum
from django.db.models.functions import Coalesce

from core.ids import new_public_id
from core.media import unique_upload_path


def exam_thumbnail_path(instance, filename):
    """Every uploaded thumbnail gets its own random name (exams/<random>.png)."""
    return unique_upload_path("exams", filename)


class Batch(models.Model):
    name = models.CharField(max_length=120, unique=True)
    description = models.TextField(blank=True)
    is_active = models.BooleanField(default=True)
    students = models.ManyToManyField(
        settings.AUTH_USER_MODEL, blank=True, related_name="student_batches",
        limit_choices_to={"role": "student"},
    )
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["name"]

    def __str__(self):
        return self.name


class ExamGroup(models.Model):
    """A family of exams that students think of together, for example SSC (MTS, CHSL, CGL) or Railways (NTPC, Group D)."""

    public_id = models.CharField(max_length=24, unique=True, editable=False, default=new_public_id)
    name = models.CharField(max_length=120, unique=True)
    description = models.TextField(blank=True)
    order = models.PositiveIntegerField(default=0)  # lower comes first; ties by name
    is_active = models.BooleanField(default=True)

    class Meta:
        ordering = ["order", "name"]

    def __str__(self):
        return self.name


class ExamCategory(models.Model):
    """What a student prepares for, for example SSC. Its subjects are the sections of that exam."""

    # Students see this in URLs instead of the sequential database id.
    public_id = models.CharField(max_length=24, unique=True, editable=False, default=new_public_id)
    name = models.CharField(max_length=120, unique=True)
    description = models.TextField(blank=True)
    subjects = models.ManyToManyField("questionbank.Subject", blank=True, related_name="exam_categories")
    # The family this exam belongs to (SSC for SSC CGL). Optional, so older exams keep working on their own.
    group = models.ForeignKey(ExamGroup, null=True, blank=True, on_delete=models.SET_NULL, related_name="categories")
    # This exam's usual pattern, used to fill in a new test: duration_minutes, marks_per_question, passing_marks,
    # negative_option / negative_value, sectional_timing, sections [{name, duration_minutes}], max_attempts.
    pattern = models.JSONField(default=dict, blank=True)
    # The tabs students see inside this exam: [{key, label, visible}] in order. Empty means the four standard tabs. See exams/tabs.py.
    tabs = models.JSONField(default=list, blank=True)
    is_active = models.BooleanField(default=True)

    class Meta:
        ordering = ["name"]
        verbose_name_plural = "exam categories"

    def __str__(self):
        return self.name


class ExamQuerySet(models.QuerySet):
    def in_category(self, category):
        """Tests of an exam: the ones made for it plus the ones also shown in it (a Maths test shared by CHSL and CGL)."""
        return self.filter(Q(category=category) | Q(also_in=category)).distinct()

    def with_totals(self):
        """Annotates total marks and question count with subqueries, so list pages do not query once per test
        and joins elsewhere in the query cannot inflate the numbers."""
        items = ExamQuestion.objects.filter(section__exam=OuterRef("pk")).order_by().values("section__exam")
        return self.annotate(
            _total_marks=Coalesce(
                Subquery(items.annotate(t=Sum("marks")).values("t")), Decimal("0"),
                output_field=models.DecimalField(max_digits=10, decimal_places=2),
            ),
            _question_count=Coalesce(Subquery(items.annotate(c=Count("id")).values("c")), 0, output_field=IntegerField()),
        )


class Exam(models.Model):
    """A test. Named Exam internally; the UI calls it a test."""

    class Status(models.TextChoices):
        DRAFT = "draft", "Draft"
        PUBLISHED = "published", "Published"
        ARCHIVED = "archived", "Archived"

    class Access(models.TextChoices):
        PUBLIC = "public", "Public (any student)"
        ASSIGNED = "assigned", "Assigned students"
        BATCH = "batch", "Batches"

    class ResultRelease(models.TextChoices):
        IMMEDIATE = "immediate", "Immediately after submission"
        SCHEDULED = "scheduled", "At a scheduled time"
        MANUAL = "manual", "Manually by an admin"

    class SolutionRelease(models.TextChoices):
        WITH_RESULT = "with_result", "Together with the result"
        AFTER_END = "after_end", "After the test window closes"
        MANUAL = "manual", "Manually by an admin"

    class TestType(models.TextChoices):
        FULL = "full_test", "Full test"
        MINI_MOCK = "mini_mock", "Mini mock"
        SECTIONAL = "sectional", "Subject test"
        PREVIOUS_YEAR = "previous_year", "Previous year paper"

    # A private practice set a student built for themselves (weak topics, wrong answers, the daily quiz). It runs through the
    # normal exam screen but never appears in admin lists, public pages or other students' lists.
    is_practice = models.BooleanField(default=False, db_index=True)
    practice_kind = models.CharField(max_length=10, blank=True)  # weak | wrong | daily
    owner = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.CASCADE, related_name="practice_exams")

    public_id = models.CharField(max_length=24, unique=True, editable=False, default=new_public_id)
    title = models.CharField(max_length=200)
    description = models.TextField(blank=True)
    instructions = models.TextField(blank=True)
    category = models.ForeignKey(ExamCategory, null=True, blank=True, on_delete=models.SET_NULL, related_name="exams")
    test_type = models.CharField(max_length=14, choices=TestType.choices, default=TestType.FULL, db_index=True)
    # Only for sectional tests (for example "Maths only") and previous year papers respectively.
    subject = models.ForeignKey("questionbank.Subject", null=True, blank=True, on_delete=models.SET_NULL, related_name="exams")
    year = models.PositiveSmallIntegerField(null=True, blank=True)
    # One of the exam's own tabs (see exams/tabs.py) to show this test in, instead of the tab of its type. Empty: by type.
    tab_key = models.CharField(max_length=20, blank=True, default="")
    thumbnail = models.ImageField(upload_to=exam_thumbnail_path, null=True, blank=True)

    class NegativeMode(models.TextChoices):
        QUESTION = "question", "Each question's own value"
        FRACTION = "fraction", "Fraction of the question's marks"
        FIXED = "fixed", "Fixed marks per wrong answer"

    duration_minutes = models.PositiveIntegerField(default=60)
    passing_marks = models.DecimalField(max_digits=8, decimal_places=2, default=0)
    # Marks every question is worth in this test (SSC: 2, RRB: 1). Null on older tests whose questions carry their own marks.
    marks_per_question = models.DecimalField(max_digits=6, decimal_places=2, null=True, blank=True)
    negative_marking_enabled = models.BooleanField(default=True)
    # How a wrong answer is penalised when negative marking is on. One rule for the whole test, so a test can say
    # "1/4 of the marks" or "0.45 per wrong answer" without editing every question.
    negative_mode = models.CharField(max_length=10, choices=NegativeMode.choices, default=NegativeMode.QUESTION)
    negative_value = models.DecimalField(max_digits=7, decimal_places=4, default=0)  # fraction (0 to 1) or fixed marks
    # Marks deducted for an unanswered question (normally 0).
    unanswered_penalty = models.DecimalField(max_digits=6, decimal_places=2, default=0)
    # SSC-style: every section has its own time limit, sections open one after another and cannot be revisited.
    sectional_timing = models.BooleanField(default=False)
    shuffle_questions = models.BooleanField(default=False)
    shuffle_options = models.BooleanField(default=False)
    max_attempts = models.PositiveIntegerField(default=1)  # 0 = unlimited retakes
    # A live test: everyone starts together at start_at and everyone ends together, start_at + duration. Joining stays open for
    # late_join_minutes after the start, and a late joiner gets the time that is left, not the full duration.
    is_live = models.BooleanField(default=False, db_index=True)
    late_join_minutes = models.PositiveSmallIntegerField(default=10, validators=[MaxValueValidator(180)])
    # Other exams that also show this test, besides its own exam (category). One test, one set of results.
    also_in = models.ManyToManyField("ExamCategory", blank=True, related_name="shared_exams")
    # A student may pause a running attempt: the clock stops, the questions are hidden, and the test resumes with the same
    # time left. A pause ends by itself after pause_minutes so nobody can hold a test open for ever.
    allow_pause = models.BooleanField(default=True)
    max_pauses = models.PositiveSmallIntegerField(default=0, validators=[MaxValueValidator(20)])  # 0 = unlimited pauses
    pause_minutes = models.PositiveSmallIntegerField(default=0, validators=[MaxValueValidator(120)])  # 0 = no limit: resume any time

    start_at = models.DateTimeField(null=True, blank=True)
    end_at = models.DateTimeField(null=True, blank=True)

    access_type = models.CharField(max_length=10, choices=Access.choices, default=Access.PUBLIC, db_index=True)
    # Premium tests are visible to everyone but can only be started by students an admin has given access to (directly, through a
    # batch, or by assigning the test). There is no payment gateway: the price is only shown, and access is granted by hand.
    is_premium = models.BooleanField(default=False, db_index=True)
    price = models.DecimalField(max_digits=8, decimal_places=2, null=True, blank=True)
    premium_students = models.ManyToManyField(settings.AUTH_USER_MODEL, blank=True, related_name="premium_exams", limit_choices_to={"role": "student"})
    premium_batches = models.ManyToManyField("Batch", blank=True, related_name="premium_exams")
    assigned_students = models.ManyToManyField(
        settings.AUTH_USER_MODEL, blank=True, related_name="assigned_exams",
        limit_choices_to={"role": "student"},
    )
    assigned_batches = models.ManyToManyField(Batch, blank=True, related_name="exams")

    status = models.CharField(max_length=10, choices=Status.choices, default=Status.DRAFT, db_index=True)
    published_at = models.DateTimeField(null=True, blank=True)
    # Bumped each time the exam is (re)published so attempts can record which rules they ran under.
    version = models.PositiveIntegerField(default=1)

    result_release = models.CharField(max_length=10, choices=ResultRelease.choices, default=ResultRelease.IMMEDIATE)
    result_release_at = models.DateTimeField(null=True, blank=True)
    results_released = models.BooleanField(default=False)  # set by manual release
    solution_release = models.CharField(max_length=12, choices=SolutionRelease.choices, default=SolutionRelease.WITH_RESULT)
    solutions_released = models.BooleanField(default=False)  # set by manual release

    created_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, on_delete=models.SET_NULL, related_name="+")
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    objects = ExamQuerySet.as_manager()

    class Meta:
        ordering = ["-created_at", "-id"]

    def __str__(self):
        return self.title

    @property
    def total_marks(self) -> Decimal:
        if hasattr(self, "_total_marks"):  # present on list queries, see ExamQuerySet.with_totals
            return Decimal(self._total_marks).quantize(Decimal("0.01"))
        total = ExamQuestion.objects.filter(section__exam=self).aggregate(t=Sum("marks"))["t"] or Decimal("0")
        return Decimal(total).quantize(Decimal("0.01"))

    @property
    def question_count(self) -> int:
        if hasattr(self, "_question_count"):
            return self._question_count
        return ExamQuestion.objects.filter(section__exam=self).count()

    def sync_section_duration(self) -> None:
        """For a test with timed sections the total duration is simply the sum of the section time limits."""
        if self.sectional_timing:
            total = sum(s.duration_minutes or 0 for s in self.sections.all())
            if total > 0 and total != self.duration_minutes:
                self.duration_minutes = total
                self.save(update_fields=["duration_minutes", "updated_at"])

    def negative_for(self, marks: Decimal, question_negative: Decimal) -> Decimal:
        """The deduction for a wrong answer on a question worth `marks`, under this test's rule."""
        if self.negative_mode == self.NegativeMode.FRACTION:
            return (Decimal(marks) * self.negative_value).quantize(Decimal("0.0001"))
        if self.negative_mode == self.NegativeMode.FIXED:
            return Decimal(self.negative_value).quantize(Decimal("0.0001"))
        return Decimal(question_negative)

    def apply_negative_rule(self) -> int:
        """Recomputes every question's marks (when the test sets them) and deduction. Returns how many changed."""
        items = list(ExamQuestion.objects.filter(section__exam=self).select_related("question"))
        changed = []
        for it in items:
            marks = self.marks_per_question if self.marks_per_question is not None else it.marks
            new = self.negative_for(marks, it.question.negative_marks)
            if new != it.negative_marks or marks != it.marks:
                it.marks, it.negative_marks = marks, new
                changed.append(it)
        ExamQuestion.objects.bulk_update(changed, ["marks", "negative_marks"])
        return len(changed)

    def premium_allows(self, user) -> bool:
        """True for a free test, or when this student has been given access to the premium test."""
        if not self.is_premium:
            return True
        return (
            self.premium_students.filter(pk=user.pk).exists()
            or self.premium_batches.filter(students=user, is_active=True).exists()
            or self.assigned_students.filter(pk=user.pk).exists()
            or self.assigned_batches.filter(students=user, is_active=True).exists()
        )

    def has_attempts(self) -> bool:
        """Once students have attempted, scoring rules and questions must stay frozen."""
        return self.attempts.exists()


class ExamSection(models.Model):
    exam = models.ForeignKey(Exam, on_delete=models.CASCADE, related_name="sections")
    name = models.CharField(max_length=120)
    order = models.PositiveIntegerField(default=0)
    duration_minutes = models.PositiveIntegerField(null=True, blank=True)  # used only when the test has sectional timing

    class Meta:
        ordering = ["order", "id"]


class ExamQuestion(models.Model):
    section = models.ForeignKey(ExamSection, on_delete=models.CASCADE, related_name="items")
    question = models.ForeignKey("questionbank.Question", on_delete=models.PROTECT, related_name="exam_items")
    order = models.PositiveIntegerField(default=0)
    # Snapshot at the time the question was added; later bank edits do not change the exam's scoring.
    marks = models.DecimalField(max_digits=6, decimal_places=2)
    negative_marks = models.DecimalField(max_digits=8, decimal_places=4, default=0)
    question_version = models.PositiveIntegerField(default=1)

    class Meta:
        ordering = ["section__order", "order", "id"]


class Preparation(models.Model):
    """An exam a student has chosen to prepare for."""

    student = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="preparations")
    category = models.ForeignKey(ExamCategory, on_delete=models.CASCADE, related_name="preparing")
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["created_at", "id"]
        constraints = [models.UniqueConstraint(fields=["student", "category"], name="uniq_preparation")]
