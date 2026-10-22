from decimal import Decimal

from django.db import transaction
from django.utils import timezone
from rest_framework import serializers

from accounts.models import Role, User
from questionbank.models import Question

from questionbank.models import Subject

from .models import Batch, Exam, ExamCategory, ExamQuestion, ExamSection

MAX_QUESTIONS_PER_TEST = 300

# Fields that define how an exam is taken or scored. Frozen once the exam leaves draft.
LOCKED_WHEN_PUBLISHED = (
    "duration_minutes", "passing_marks", "marks_per_question", "negative_marking_enabled", "negative_mode", "negative_value", "unanswered_penalty",
    "sectional_timing", "shuffle_questions", "shuffle_options", "max_attempts", "access_type",
)


class BatchSerializer(serializers.ModelSerializer):
    member_count = serializers.IntegerField(read_only=True)

    class Meta:
        model = Batch
        fields = ["id", "name", "description", "is_active", "member_count", "created_at"]
        read_only_fields = ["id", "created_at"]

    def validate_name(self, value):
        value = value.strip()
        qs = Batch.objects.filter(name__iexact=value)
        if self.instance:
            qs = qs.exclude(pk=self.instance.pk)
        if qs.exists():
            raise serializers.ValidationError("A batch with this name already exists.")
        return value


class ExamCategorySerializer(serializers.ModelSerializer):
    exam_count = serializers.IntegerField(read_only=True)
    subjects = serializers.PrimaryKeyRelatedField(many=True, queryset=Subject.objects.all(), required=False)
    subject_names = serializers.SerializerMethodField()

    class Meta:
        model = ExamCategory
        fields = ["id", "name", "description", "subjects", "subject_names", "is_active", "exam_count"]

    def get_subject_names(self, obj):
        return [s.name for s in obj.subjects.all()]

    def validate_name(self, value):
        value = value.strip()
        qs = ExamCategory.objects.filter(name__iexact=value)
        if self.instance:
            qs = qs.exclude(pk=self.instance.pk)
        if qs.exists():
            raise serializers.ValidationError("A category with this name already exists.")
        return value


class ExamQuestionReadSerializer(serializers.ModelSerializer):
    text = serializers.CharField(source="question.display_text", read_only=True)
    subject_name = serializers.CharField(source="question.subject.name", read_only=True)
    difficulty = serializers.CharField(source="question.difficulty", read_only=True)
    is_active = serializers.BooleanField(source="question.is_active", read_only=True)
    current_version = serializers.IntegerField(source="question.version", read_only=True)

    class Meta:
        model = ExamQuestion
        fields = [
            "id", "question", "text", "subject_name", "difficulty", "is_active", "order",
            "marks", "negative_marks", "question_version", "current_version",
        ]


class ExamSectionReadSerializer(serializers.ModelSerializer):
    questions = ExamQuestionReadSerializer(source="items", many=True, read_only=True)

    class Meta:
        model = ExamSection
        fields = ["id", "name", "order", "duration_minutes", "questions"]


class ExamSerializer(serializers.ModelSerializer):
    total_marks = serializers.DecimalField(max_digits=10, decimal_places=2, read_only=True)
    question_count = serializers.IntegerField(read_only=True)
    category_name = serializers.CharField(source="category.name", read_only=True, default=None)
    subject_name = serializers.CharField(source="subject.name", read_only=True, default=None)
    thumbnail_url = serializers.SerializerMethodField()
    assigned_student_ids = serializers.PrimaryKeyRelatedField(source="assigned_students", many=True, read_only=True)
    assigned_batch_ids = serializers.PrimaryKeyRelatedField(source="assigned_batches", many=True, read_only=True)
    locked = serializers.SerializerMethodField()

    class Meta:
        model = Exam
        fields = [
            "id", "public_id", "title", "description", "instructions", "category", "category_name", "thumbnail_url",
            "test_type", "subject", "subject_name", "year",
            "duration_minutes", "passing_marks", "marks_per_question", "negative_marking_enabled", "negative_mode", "negative_value",
            "unanswered_penalty", "sectional_timing", "shuffle_questions", "shuffle_options", "max_attempts", "start_at", "end_at",
            "access_type", "assigned_student_ids", "assigned_batch_ids",
            "status", "published_at", "version", "locked",
            "result_release", "result_release_at", "results_released",
            "solution_release", "solutions_released",
            "total_marks", "question_count", "created_at", "updated_at",
        ]
        read_only_fields = ["id", "public_id", "status", "published_at", "version", "results_released", "solutions_released",
                            "created_at", "updated_at"]

    def get_thumbnail_url(self, obj):
        if not obj.thumbnail:
            return None
        request = self.context.get("request")
        return request.build_absolute_uri(obj.thumbnail.url) if request else obj.thumbnail.url

    def get_locked(self, obj):
        return obj.status != Exam.Status.DRAFT

    def to_representation(self, instance):
        data = super().to_representation(instance)
        # Annotated values may be absent on detail/create responses.
        data["total_marks"] = str(instance.total_marks)
        data["question_count"] = instance.question_count
        return data

    def validate_duration_minutes(self, v):
        if not 1 <= v <= 600:
            raise serializers.ValidationError("Duration must be between 1 and 600 minutes.")
        return v

    def validate_max_attempts(self, v):
        if not 0 <= v <= 50:
            raise serializers.ValidationError("Attempts must be between 1 and 50, or 0 for unlimited.")
        return v

    def validate_marks_per_question(self, v):
        if v is not None and v <= 0:
            raise serializers.ValidationError("Marks per question must be greater than zero.")
        return v

    def validate_passing_marks(self, v):
        if v < 0:
            raise serializers.ValidationError("Passing marks cannot be negative.")
        return v

    def validate_unanswered_penalty(self, v):
        if v < 0:
            raise serializers.ValidationError("Enter the deduction as a positive number (or 0).")
        return v

    def validate(self, attrs):
        inst = self.instance
        get = lambda k: attrs.get(k, getattr(inst, k, None))  # noqa: E731

        if inst and inst.status == Exam.Status.ARCHIVED:
            raise serializers.ValidationError("Archived tests cannot be edited. Restore it first.")
        if inst and inst.status != Exam.Status.DRAFT:
            changed = [f for f in LOCKED_WHEN_PUBLISHED if f in attrs and attrs[f] != getattr(inst, f)]
            if changed:
                raise serializers.ValidationError(
                    {f: "Locked while the test is published. Unpublish it to change this." for f in changed}
                )

        kind = get("test_type")
        if kind == Exam.TestType.SECTIONAL:
            if not get("subject"):
                raise serializers.ValidationError({"subject": "Choose the subject for a subject test."})
        else:
            attrs["subject"] = None
        if kind == Exam.TestType.PREVIOUS_YEAR:
            year = get("year")
            if not year or not 1950 <= year <= timezone.now().year + 1:
                raise serializers.ValidationError({"year": "Enter the year of the paper, for example 2023."})
        else:
            attrs["year"] = None

        mode, value = get("negative_mode") or Exam.NegativeMode.QUESTION, get("negative_value")
        value = Decimal("0") if value is None else Decimal(value)
        if mode == Exam.NegativeMode.FRACTION and not Decimal("0") < value <= Decimal("1"):
            raise serializers.ValidationError({"negative_value": "Enter a fraction between 0 and 1, for example 0.25 for 1/4."})
        if mode == Exam.NegativeMode.FIXED and not Decimal("0") <= value <= Decimal("100"):
            raise serializers.ValidationError({"negative_value": "Enter the marks deducted per wrong answer (0 or more)."})
        if mode == Exam.NegativeMode.QUESTION:
            attrs["negative_value"] = Decimal("0")  # not used; keep the stored value tidy

        start, end = get("start_at"), get("end_at")
        if start and end and end <= start:
            raise serializers.ValidationError({"end_at": "End must be after the start."})
        if get("result_release") == Exam.ResultRelease.SCHEDULED and not get("result_release_at"):
            raise serializers.ValidationError({"result_release_at": "Choose when results are released."})
        return attrs


# --- structure (sections + questions) --------------------------------------------------------

class StructureQuestionSerializer(serializers.Serializer):
    question = serializers.PrimaryKeyRelatedField(queryset=Question.objects.all())
    marks = serializers.DecimalField(max_digits=6, decimal_places=2, required=False, min_value=Decimal("0.01"))
    negative_marks = serializers.DecimalField(max_digits=8, decimal_places=4, required=False, min_value=Decimal("0"))


class StructureSectionSerializer(serializers.Serializer):
    name = serializers.CharField(max_length=120)
    duration_minutes = serializers.IntegerField(required=False, allow_null=True, min_value=1, max_value=600)
    questions = StructureQuestionSerializer(many=True, allow_empty=True)


class StructureSerializer(serializers.Serializer):
    sections = StructureSectionSerializer(many=True, allow_empty=False, max_length=30)

    def validate_sections(self, sections):
        total = sum(len(s["questions"]) for s in sections)
        if total > MAX_QUESTIONS_PER_TEST:
            raise serializers.ValidationError(f"A test can have at most {MAX_QUESTIONS_PER_TEST} questions (this one has {total}).")
        seen = set()
        names = set()
        for s in sections:
            lname = s["name"].strip().lower()
            if lname in names:
                raise serializers.ValidationError(f"Duplicate section name: {s['name']}.")
            names.add(lname)
            for item in s["questions"]:
                q = item["question"]
                if q.id in seen:
                    raise serializers.ValidationError(f"Question #{q.id} appears more than once.")
                seen.add(q.id)
                if not q.is_active:
                    raise serializers.ValidationError(f"Question #{q.id} is inactive and cannot be added.")
        return sections

    @transaction.atomic
    def save(self, exam: Exam):
        exam.sections.all().delete()
        for si, s in enumerate(self.validated_data["sections"]):
            section = ExamSection.objects.create(
                exam=exam, name=s["name"].strip(), order=si, duration_minutes=s.get("duration_minutes")
            )
            for qi, item in enumerate(s["questions"]):
                q = item["question"]
                marks = exam.marks_per_question if exam.marks_per_question is not None else item.get("marks", q.marks)
                ExamQuestion.objects.create(
                    section=section, question=q, order=qi, marks=marks,
                    # A fraction or fixed rule set on the test decides the deduction; otherwise the question's own applies.
                    negative_marks=(
                        exam.negative_for(marks, q.negative_marks)
                        if exam.negative_mode != Exam.NegativeMode.QUESTION
                        else item.get("negative_marks", q.negative_marks)
                    ),
                    question_version=q.version,
                )
        if exam.sectional_timing:
            total = sum(s.get("duration_minutes") or 0 for s in self.validated_data["sections"])
            if total > 600:
                raise serializers.ValidationError({"sections": f"The section time limits add up to {total} minutes; the most a test can last is 600."})
            exam.sync_section_duration()
        return exam


class AssignmentSerializer(serializers.Serializer):
    students = serializers.PrimaryKeyRelatedField(
        queryset=User.objects.filter(role=Role.STUDENT), many=True, required=False
    )
    batches = serializers.PrimaryKeyRelatedField(queryset=Batch.objects.all(), many=True, required=False)
