from datetime import timedelta
from decimal import Decimal

from django.db import transaction
from django.utils import timezone
from rest_framework import serializers

from accounts.models import Role, User
from questionbank.models import Question

from questionbank.models import Subject

from .tabs import LABEL_MAX, MAX_TABS, TAB_KEYS, custom_keys, is_custom, resolve_tabs
from .models import Batch, Exam, ExamCategory, ExamGroup, ExamQuestion, ExamSection

MAX_QUESTIONS_PER_TEST = 300

# Fields that define how an exam is taken or scored. Frozen once the exam leaves draft.
LOCKED_WHEN_PUBLISHED = (
    "duration_minutes", "passing_marks", "marks_per_question", "negative_marking_enabled", "negative_mode", "negative_value", "unanswered_penalty",
    "sectional_timing", "shuffle_questions", "shuffle_options", "max_attempts", "access_type", "is_live", "late_join_minutes",
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


NEGATIVE_OPTIONS = ("off", "question", "fraction", "fixed")


def clean_pattern(value) -> dict:
    """Checks an exam's pattern and keeps only the settings we know. An empty pattern means "none saved"."""
    if value in (None, "", {}):
        return {}
    if not isinstance(value, dict):
        raise serializers.ValidationError("The pattern must be a set of settings.")

    def number(key, low, high, whole=False):
        raw = value.get(key)
        if raw in (None, ""):
            return None
        try:
            n = Decimal(str(raw))
        except Exception:  # noqa: BLE001
            raise serializers.ValidationError({key: "Enter a number."})
        if not Decimal(str(low)) <= n <= Decimal(str(high)) or (whole and n != n.to_integral_value()):
            raise serializers.ValidationError({key: f"Enter {'a whole number' if whole else 'a number'} from {low} to {high}."})
        return int(n) if whole else float(n)

    out = {}
    for key, low, high, whole in (
        ("duration_minutes", 1, 600, True), ("marks_per_question", 0.01, 100, False),
        ("passing_marks", 0, 10000, False), ("negative_value", 0, 100, False), ("max_attempts", 0, 50, True),
    ):
        n = number(key, low, high, whole)
        if n is not None:
            out[key] = n
    option = value.get("negative_option")
    if option not in (None, ""):
        if option not in NEGATIVE_OPTIONS:
            raise serializers.ValidationError({"negative_option": "Choose off, per question, a fraction or a fixed mark."})
        out["negative_option"] = option
    if "sectional_timing" in value:
        out["sectional_timing"] = bool(value["sectional_timing"])
    sections = value.get("sections") or []
    if not isinstance(sections, list) or len(sections) > 30:
        raise serializers.ValidationError({"sections": "Send up to 30 sections."})
    cleaned = []
    for sec in sections:
        name = str(sec.get("name", "")).strip() if isinstance(sec, dict) else ""
        if not name or len(name) > 120:
            raise serializers.ValidationError({"sections": "Every section needs a name of up to 120 characters."})
        minutes = sec.get("duration_minutes")
        if minutes in (None, ""):
            minutes = None
        else:
            try:
                minutes = int(minutes)
            except (TypeError, ValueError):
                raise serializers.ValidationError({"sections": "Section minutes must be a whole number."})
            if not 1 <= minutes <= 600:
                raise serializers.ValidationError({"sections": "Section minutes must be between 1 and 600."})
        cleaned.append({"name": name, "duration_minutes": minutes})
    if cleaned:
        out["sections"] = cleaned
    return out


class ExamGroupSerializer(serializers.ModelSerializer):
    exam_count = serializers.IntegerField(read_only=True, default=0)

    class Meta:
        model = ExamGroup
        fields = ["id", "public_id", "name", "description", "order", "is_active", "exam_count"]
        read_only_fields = ["id", "public_id"]

    def validate_name(self, value):
        value = value.strip()
        qs = ExamGroup.objects.filter(name__iexact=value)
        if self.instance:
            qs = qs.exclude(pk=self.instance.pk)
        if qs.exists():
            raise serializers.ValidationError("A family with this name already exists.")
        return value


class ExamCategorySerializer(serializers.ModelSerializer):
    exam_count = serializers.IntegerField(read_only=True)
    subjects = serializers.PrimaryKeyRelatedField(many=True, queryset=Subject.objects.all(), required=False)
    subject_names = serializers.SerializerMethodField()
    group = serializers.PrimaryKeyRelatedField(queryset=ExamGroup.objects.all(), required=False, allow_null=True)
    group_name = serializers.CharField(source="group.name", read_only=True, default=None)
    pattern = serializers.JSONField(required=False)
    tabs = serializers.JSONField(required=False)

    class Meta:
        model = ExamCategory
        fields = ["id", "name", "description", "subjects", "subject_names", "group", "group_name", "pattern", "tabs", "is_active", "exam_count"]

    def get_subject_names(self, obj):
        return [s.name for s in obj.subjects.all()]

    def validate_pattern(self, value):
        return clean_pattern(value)

    def validate_tabs(self, value):
        """A list of {key, label, visible}: the four built-in tabs plus any of the admin's own. At least one tab must show."""
        if value in (None, "", []):
            return []
        if not isinstance(value, list):
            raise serializers.ValidationError("Send the tabs as a list.")
        seen = set()
        for item in value:
            if not isinstance(item, dict) or not (item.get("key") in TAB_KEYS or is_custom(item.get("key"))):
                raise serializers.ValidationError("Unknown tab.")
            if item["key"] in seen:
                raise serializers.ValidationError("A tab is listed twice.")
            seen.add(item["key"])
            label = str(item.get("label") or "").strip()
            if len(label) > LABEL_MAX:
                raise serializers.ValidationError(f"A tab name can be at most {LABEL_MAX} characters.")
            if is_custom(item["key"]) and not label:
                raise serializers.ValidationError("Give every new tab a name.")
        if len(resolve_tabs(value)) > MAX_TABS:
            raise serializers.ValidationError(f"An exam can have at most {MAX_TABS} tabs.")
        if not any(item.get("visible") is not False for item in value):
            raise serializers.ValidationError("At least one tab must be shown.")
        return resolve_tabs(value)

    def to_representation(self, instance):
        data = super().to_representation(instance)
        data["tabs"] = resolve_tabs(instance.tabs)  # always the four, in order, so the admin screen can show them all
        return data

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
    question_date = serializers.DateField(source="question.question_date", read_only=True)
    subject_name = serializers.CharField(source="question.subject.name", read_only=True)
    difficulty = serializers.CharField(source="question.difficulty", read_only=True)
    is_active = serializers.BooleanField(source="question.is_active", read_only=True)
    current_version = serializers.IntegerField(source="question.version", read_only=True)
    also_in = serializers.SerializerMethodField()

    def get_also_in(self, obj):
        """Titles of the other tests (not archived) that use this question."""
        titles = {
            i.section.exam.title for i in obj.question.exam_items.all()
            if i.section.exam_id != obj.section.exam_id and i.section.exam.status != "archived"
        }
        return sorted(titles)

    class Meta:
        model = ExamQuestion
        fields = [
            "id", "question", "text", "question_date", "subject_name", "difficulty", "is_active", "order",
            "marks", "negative_marks", "question_version", "current_version", "also_in",
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
    premium_student_ids = serializers.PrimaryKeyRelatedField(source="premium_students", many=True, read_only=True)
    premium_batch_ids = serializers.PrimaryKeyRelatedField(source="premium_batches", many=True, read_only=True)
    locked = serializers.SerializerMethodField()
    # Other exams that also show this test. Its own exam (category) is left out of the list automatically.
    also_in = serializers.PrimaryKeyRelatedField(many=True, queryset=ExamCategory.objects.all(), required=False)

    class Meta:
        model = Exam
        fields = [
            "id", "public_id", "title", "description", "instructions", "category", "category_name", "also_in", "thumbnail_url",
            "test_type", "tab_key", "subject", "subject_name", "year",
            "duration_minutes", "passing_marks", "marks_per_question", "negative_marking_enabled", "negative_mode", "negative_value",
            "unanswered_penalty", "sectional_timing", "shuffle_questions", "shuffle_options", "max_attempts", "start_at", "end_at",
            "allow_pause", "max_pauses", "pause_minutes", "is_live", "late_join_minutes",
            "access_type", "assigned_student_ids", "assigned_batch_ids", "is_premium", "price", "premium_student_ids", "premium_batch_ids",
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

    def validate_price(self, v):
        if v is not None and v < 0:
            raise serializers.ValidationError("The price cannot be negative.")
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

        if "also_in" in attrs:
            own = attrs["category"] if "category" in attrs else (inst.category if inst else None)
            attrs["also_in"] = [c for c in attrs["also_in"] if c != own]

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

        tab_key = (get("tab_key") or "").strip()
        if tab_key:
            category = attrs["category"] if "category" in attrs else (inst.category if inst else None)
            if category is None or tab_key not in custom_keys(category.tabs):
                raise serializers.ValidationError({"tab_key": "Choose one of this exam's own tabs, or leave it empty."})
        attrs["tab_key"] = tab_key

        mode, value = get("negative_mode") or Exam.NegativeMode.QUESTION, get("negative_value")
        value = Decimal("0") if value is None else Decimal(value)
        if mode == Exam.NegativeMode.FRACTION and not Decimal("0") < value <= Decimal("1"):
            raise serializers.ValidationError({"negative_value": "Enter a fraction between 0 and 1, for example 0.25 for 1/4."})
        if mode == Exam.NegativeMode.FIXED and not Decimal("0") <= value <= Decimal("100"):
            raise serializers.ValidationError({"negative_value": "Enter the marks deducted per wrong answer (0 or more)."})
        if mode == Exam.NegativeMode.QUESTION:
            attrs["negative_value"] = Decimal("0")  # not used; keep the stored value tidy

        if get("is_live"):
            start = get("start_at")
            if not start:
                raise serializers.ValidationError({"start_at": "A live test needs a start time."})
            # Everyone ends together, so the end of the window is simply start + duration; it is worked out, not typed.
            attrs["end_at"] = start + timedelta(minutes=get("duration_minutes") or 0)
            attrs["allow_pause"] = False  # a pause would give some students more time than others

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
