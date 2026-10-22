from PIL import Image
from django.db import transaction
from django.db.models import Count, Prefetch
from django.utils import timezone
from rest_framework import serializers, status, viewsets
from rest_framework.decorators import action
from rest_framework.parsers import FormParser, JSONParser, MultiPartParser
from rest_framework.response import Response

from accounts.models import Role, User, log_activity
from core.ids import new_public_id
from core.numbers import money
from accounts.permissions import IsAdminOrSuperAdmin
from accounts.serializers import UserSerializer

from .models import Batch, Exam, ExamCategory, ExamQuestion, ExamSection
from .serializers import (
    AssignmentSerializer, BatchSerializer, ExamCategorySerializer, ExamSectionReadSerializer,
    ExamSerializer, StructureSerializer,
)

MAX_IMAGE_BYTES = 2 * 1024 * 1024


class BatchViewSet(viewsets.ModelViewSet):
    serializer_class = BatchSerializer
    permission_classes = [IsAdminOrSuperAdmin]
    filterset_fields = ["is_active"]
    search_fields = ["name", "description"]
    ordering_fields = ["name", "created_at"]

    def get_queryset(self):
        return Batch.objects.annotate(member_count=Count("students", distinct=True)).order_by("name")

    def perform_create(self, serializer):
        log_activity(self.request.user, "batch.create", serializer.save())

    def perform_update(self, serializer):
        log_activity(self.request.user, "batch.update", serializer.save())

    def destroy(self, request, *args, **kwargs):
        batch = self.get_object()
        if batch.exams.exists():
            return Response(
                {"detail": "This batch is assigned to tests and cannot be deleted. Deactivate it instead."},
                status=status.HTTP_409_CONFLICT,
            )
        log_activity(request.user, "batch.delete", None, batch.name)
        batch.delete()
        return Response(status=status.HTTP_204_NO_CONTENT)

    @action(detail=True, methods=["get"])
    def members(self, request, pk=None):
        batch = self.get_object()
        students = batch.students.order_by("first_name", "username")
        return Response(UserSerializer(students, many=True).data)

    @action(detail=True, methods=["post"], url_path="add-members")
    def add_members(self, request, pk=None):
        batch = self.get_object()
        ids = request.data.get("student_ids") or []
        students = User.objects.filter(pk__in=ids, role=Role.STUDENT)
        batch.students.add(*students)
        log_activity(request.user, "batch.add_members", batch, f"{students.count()} students")
        return Response({"member_count": batch.students.count()})

    @action(detail=True, methods=["post"], url_path="remove-members")
    def remove_members(self, request, pk=None):
        batch = self.get_object()
        ids = request.data.get("student_ids") or []
        batch.students.remove(*User.objects.filter(pk__in=ids))
        log_activity(request.user, "batch.remove_members", batch, f"{len(ids)} students")
        return Response({"member_count": batch.students.count()})


class ExamCategoryViewSet(viewsets.ModelViewSet):
    serializer_class = ExamCategorySerializer
    permission_classes = [IsAdminOrSuperAdmin]
    filterset_fields = ["is_active"]
    search_fields = ["name"]

    def get_queryset(self):
        return ExamCategory.objects.annotate(exam_count=Count("exams", distinct=True)).prefetch_related("subjects").order_by("name")

    def perform_create(self, serializer):
        log_activity(self.request.user, "exam_category.create", serializer.save())

    def perform_update(self, serializer):
        log_activity(self.request.user, "exam_category.update", serializer.save())

    def destroy(self, request, *args, **kwargs):
        cat = self.get_object()
        if cat.exams.exists():
            return Response(
                {"detail": "Tests belong to this exam, so it cannot be deleted. Mark it inactive instead."},
                status=status.HTTP_409_CONFLICT,
            )
        log_activity(request.user, "exam_category.delete", None, cat.name)
        cat.delete()
        return Response(status=status.HTTP_204_NO_CONTENT)


def publish_problems(exam: Exam) -> list[str]:
    """Everything that must be true before students can see a test."""
    problems = []
    items = list(ExamQuestion.objects.filter(section__exam=exam).select_related("question").prefetch_related("question__options"))
    if not items:
        problems.append("Add at least one question.")
    if any(not s.items.exists() for s in exam.sections.all()):
        problems.append("Remove or fill empty sections.")
    for it in items:
        q = it.question
        if not q.is_active:
            problems.append(f"Question #{q.id} is inactive.")
        opts = list(q.options.all())
        if len(opts) < 2 or sum(o.is_correct for o in opts) != 1:
            problems.append(f"Question #{q.id} has invalid options.")
    if exam.total_marks > 0 and exam.passing_marks > exam.total_marks:
        problems.append(f"Passing marks ({exam.passing_marks}) exceed total marks ({exam.total_marks}).")
    if exam.start_at and exam.end_at:
        if exam.end_at <= timezone.now():
            problems.append("The end time is already in the past.")
        elif (exam.end_at - exam.start_at).total_seconds() < exam.duration_minutes * 60:
            problems.append("The availability window is shorter than the test duration.")
    elif exam.end_at and exam.end_at <= timezone.now():
        problems.append("The end time is already in the past.")
    if exam.sectional_timing:
        sections = list(exam.sections.all())
        for sec in sections:
            if not sec.duration_minutes:
                problems.append(f"Set a time limit (in minutes) for the section '{sec.name}'.")
        if sum(sec.duration_minutes or 0 for sec in sections) > 600:
            problems.append("The section time limits add up to more than 600 minutes.")
    if exam.access_type == Exam.Access.ASSIGNED and not exam.assigned_students.exists():
        problems.append("Assign at least one student, or change access to Public or Batches.")
    if exam.access_type == Exam.Access.BATCH and not exam.assigned_batches.exists():
        problems.append("Assign at least one batch, or change access.")
    return problems


class ExamViewSet(viewsets.ModelViewSet):
    serializer_class = ExamSerializer
    permission_classes = [IsAdminOrSuperAdmin]
    filterset_fields = ["status", "category", "access_type", "test_type", "subject"]
    search_fields = ["title", "description"]
    ordering_fields = ["created_at", "updated_at", "start_at", "title"]
    parser_classes = [JSONParser, FormParser, MultiPartParser]

    def get_queryset(self):
        qs = Exam.objects.select_related("category", "subject").prefetch_related("assigned_students", "assigned_batches")
        return qs.with_totals() if self.action == "list" else qs

    def perform_create(self, serializer):
        exam = serializer.save(created_by=self.request.user)
        log_activity(self.request.user, "exam.create", exam)

    def perform_update(self, serializer):
        before = (serializer.instance.negative_mode, serializer.instance.negative_value, serializer.instance.marks_per_question)
        exam = serializer.save()
        if (exam.negative_mode, exam.negative_value, exam.marks_per_question) != before:
            exam.apply_negative_rule()  # only when the rule itself changed, so hand-set values are not overwritten
        exam.sync_section_duration()
        log_activity(self.request.user, "exam.update", exam)

    def destroy(self, request, *args, **kwargs):
        exam = self.get_object()
        if exam.status != Exam.Status.DRAFT or exam.has_attempts():
            return Response(
                {"detail": "Only draft tests can be deleted. Archive this one instead."},
                status=status.HTTP_409_CONFLICT,
            )
        log_activity(request.user, "exam.delete", None, exam.title)
        exam.delete()
        return Response(status=status.HTTP_204_NO_CONTENT)

    def _detail(self, exam):
        data = self.get_serializer(exam).data
        data["sections"] = ExamSectionReadSerializer(
            exam.sections.prefetch_related(Prefetch("items", ExamQuestion.objects.select_related("question__subject"))),
            many=True,
        ).data
        return data

    def retrieve(self, request, *args, **kwargs):
        return Response(self._detail(self.get_object()))

    # --- lifecycle ---
    @action(detail=True, methods=["post"])
    def publish(self, request, pk=None):
        exam = self.get_object()
        if exam.status != Exam.Status.DRAFT:
            raise serializers.ValidationError({"detail": "Only draft tests can be published."})
        problems = publish_problems(exam)
        if problems:
            return Response({"detail": "This test is not ready to publish.", "errors": problems}, status=400)
        exam.sync_section_duration()
        exam.status = Exam.Status.PUBLISHED
        exam.published_at = timezone.now()
        exam.version += 1
        exam.save(update_fields=["status", "published_at", "version", "updated_at"])
        log_activity(request.user, "exam.publish", exam, f"v{exam.version}")
        return Response(self._detail(exam))

    @action(detail=True, methods=["post"])
    def unpublish(self, request, pk=None):
        exam = self.get_object()
        if exam.status != Exam.Status.PUBLISHED:
            raise serializers.ValidationError({"detail": "Only published tests can be unpublished."})
        if exam.has_attempts():
            raise serializers.ValidationError(
                {"detail": "Students have already attempted this test, so its rules are frozen. Archive it instead."}
            )
        exam.status = Exam.Status.DRAFT
        exam.save(update_fields=["status", "updated_at"])
        log_activity(request.user, "exam.unpublish", exam)
        return Response(self._detail(exam))

    @action(detail=True, methods=["post"])
    def archive(self, request, pk=None):
        exam = self.get_object()
        exam.status = Exam.Status.ARCHIVED
        exam.save(update_fields=["status", "updated_at"])
        log_activity(request.user, "exam.archive", exam)
        return Response(self._detail(exam))

    @action(detail=True, methods=["post"])
    def restore(self, request, pk=None):
        exam = self.get_object()
        if exam.status != Exam.Status.ARCHIVED:
            raise serializers.ValidationError({"detail": "Only archived tests can be restored."})
        exam.status = Exam.Status.DRAFT  # must be re-checked and re-published
        exam.save(update_fields=["status", "updated_at"])
        log_activity(request.user, "exam.restore", exam)
        return Response(self._detail(exam))

    @action(detail=True, methods=["post"])
    def duplicate(self, request, pk=None):
        src = self.get_object()
        with transaction.atomic():
            copy = Exam.objects.get(pk=src.pk)
            copy.pk = None
            copy.public_id = new_public_id()
            copy.title = f"{src.title} (copy)"[:200]
            copy.status = Exam.Status.DRAFT
            copy.published_at = None
            copy.version = 1
            copy.results_released = False
            copy.solutions_released = False
            copy.created_by = request.user
            copy.save()
            for section in src.sections.all():
                new_section = ExamSection.objects.create(
                    exam=copy, name=section.name, order=section.order, duration_minutes=section.duration_minutes
                )
                ExamQuestion.objects.bulk_create(
                    ExamQuestion(
                        section=new_section, question=i.question, order=i.order, marks=i.marks,
                        negative_marks=i.negative_marks, question_version=i.question.version,
                    )
                    for i in section.items.select_related("question")
                )
            copy.assigned_students.set(src.assigned_students.all())
            copy.assigned_batches.set(src.assigned_batches.all())
        log_activity(request.user, "exam.duplicate", copy, f"from #{src.pk}")
        return Response(self._detail(copy), status=status.HTTP_201_CREATED)

    # --- content ---
    @action(detail=True, methods=["put"])
    def structure(self, request, pk=None):
        exam = self.get_object()
        if exam.status != Exam.Status.DRAFT:
            raise serializers.ValidationError(
                {"detail": "Questions can only be changed while the test is a draft. Unpublish it first."}
            )
        ser = StructureSerializer(data=request.data)
        ser.is_valid(raise_exception=True)
        ser.save(exam)
        exam.save(update_fields=["updated_at"])
        log_activity(request.user, "exam.structure", exam, f"{exam.question_count} questions")
        return Response(self._detail(exam))

    @action(detail=True, methods=["put"])
    def assignment(self, request, pk=None):
        exam = self.get_object()
        if exam.status == Exam.Status.ARCHIVED:
            raise serializers.ValidationError({"detail": "Archived tests cannot be changed."})
        ser = AssignmentSerializer(data=request.data)
        ser.is_valid(raise_exception=True)
        if "students" in ser.validated_data:
            exam.assigned_students.set(ser.validated_data["students"])
        if "batches" in ser.validated_data:
            exam.assigned_batches.set(ser.validated_data["batches"])
        exam.save(update_fields=["updated_at"])
        log_activity(request.user, "exam.assign", exam)
        return Response(self._detail(exam))

    @action(detail=True, methods=["post", "delete"])
    def thumbnail(self, request, pk=None):
        exam = self.get_object()
        if request.method == "DELETE":
            if exam.thumbnail:
                exam.thumbnail.delete(save=True)
            return Response(self._detail(exam))
        f = request.FILES.get("image")
        if not f:
            raise serializers.ValidationError({"image": "No file uploaded."})
        if f.size > MAX_IMAGE_BYTES:
            raise serializers.ValidationError({"image": "Image must be 2 MB or smaller."})
        try:
            Image.open(f).verify()
        except Exception:
            raise serializers.ValidationError({"image": "Upload a valid image file."})
        f.seek(0)
        if exam.thumbnail:
            exam.thumbnail.delete(save=False)
        exam.thumbnail = f
        exam.save()
        return Response(self._detail(exam))

    @action(detail=True, methods=["get"])
    def preview(self, request, pk=None):
        """The test exactly as a student sees it: no correct answers, no explanations."""
        exam = self.get_object()
        sections = []
        for s in exam.sections.all():
            questions = []
            for it in s.items.select_related("question").prefetch_related("question__options"):
                q = it.question
                questions.append({
                    "id": q.id,
                    "text": q.text,
                    "text_hi": q.text_hi,
                    "image_url": request.build_absolute_uri(q.image.url) if q.image else None,
                    "marks": str(it.marks),
                    "negative_marks": money(it.negative_marks if exam.negative_marking_enabled else 0),
                    "options": [{"id": o.id, "text": o.text, "text_hi": o.text_hi} for o in q.options.all()],
                })
            sections.append({"id": s.id, "name": s.name, "duration_minutes": s.duration_minutes, "questions": questions})
        return Response({
            "title": exam.title, "description": exam.description, "instructions": exam.instructions,
            "duration_minutes": exam.duration_minutes, "total_marks": str(exam.total_marks),
            "question_count": exam.question_count, "negative_marking_enabled": exam.negative_marking_enabled,
            "max_attempts": exam.max_attempts, "start_at": exam.start_at, "end_at": exam.end_at,
            "sectional_timing": exam.sectional_timing, "sections": sections,
        })
