import django_filters
from django.db.models import Avg, Count, ExpressionWrapper, F, FloatField, Max, Q
from django.http import HttpResponse
from django.shortcuts import get_object_or_404
from rest_framework import generics, serializers
from rest_framework.response import Response
from rest_framework.views import APIView

from accounts.models import Role, User, log_activity
from accounts.permissions import IsAdminOrSuperAdmin
from exams.models import Batch, Exam

from . import pdf, stats

MAX_REPORT_STUDENTS = 200
MAX_REPORT_EXAMS = 30


class StudentFilter(django_filters.FilterSet):
    batch = django_filters.NumberFilter(method="filter_batch")
    registered_from = django_filters.DateFilter(field_name="date_joined", lookup_expr="date__gte")
    registered_to = django_filters.DateFilter(field_name="date_joined", lookup_expr="date__lte")

    class Meta:
        model = User
        fields = ["is_active"]

    def filter_batch(self, qs, name, value):
        return qs.filter(pk__in=Batch.objects.filter(pk=value).values("students"))


class StudentRowSerializer(serializers.ModelSerializer):
    name = serializers.SerializerMethodField()
    attempts_count = serializers.IntegerField(read_only=True)
    average_percentage = serializers.FloatField(read_only=True)
    last_attempt_at = serializers.DateTimeField(read_only=True)
    batches = serializers.SerializerMethodField()

    class Meta:
        model = User
        fields = [
            "id", "name", "username", "email", "phone", "is_active", "date_joined",
            "attempts_count", "average_percentage", "last_attempt_at", "batches",
        ]

    def get_name(self, u):
        return stats.name_of(u)

    def get_batches(self, u):
        return [{"id": b.id, "name": b.name} for b in u.student_batches.all()]

    def to_representation(self, u):
        data = super().to_representation(u)
        if data["average_percentage"] is not None:
            data["average_percentage"] = round(data["average_percentage"], 1)
        return data


class StudentList(generics.ListAPIView):
    permission_classes = [IsAdminOrSuperAdmin]
    serializer_class = StudentRowSerializer
    filterset_class = StudentFilter
    search_fields = ["username", "email", "first_name", "last_name", "phone"]
    ordering_fields = ["date_joined", "attempts_count", "average_percentage", "last_attempt_at", "first_name"]
    ordering = ["-date_joined"]

    def get_queryset(self):
        done = Q(attempts__status="submitted")
        pct = ExpressionWrapper(F("attempts__score") * 100.0 / F("attempts__total_marks"), output_field=FloatField())
        return (
            User.objects.filter(role=Role.STUDENT)
            .annotate(
                attempts_count=Count("attempts", filter=done, distinct=True),
                average_percentage=Avg(pct, filter=done),
                last_attempt_at=Max("attempts__submitted_at", filter=done),
            )
            .prefetch_related("student_batches")
        )


def _student(pk) -> User:
    return get_object_or_404(User, pk=pk, role=Role.STUDENT)


def _assigned_exams(student):
    return [
        {"id": e.id, "title": e.title, "status": e.status, "access_type": e.access_type}
        for e in student.assigned_exams.exclude(status=Exam.Status.ARCHIVED).order_by("title")
    ]


class StudentDetail(APIView):
    permission_classes = [IsAdminOrSuperAdmin]

    def get(self, request, pk):
        s = _student(pk)
        return Response({
            "id": s.id, "name": stats.name_of(s), "username": s.username, "email": s.email, "phone": s.phone,
            "is_active": s.is_active, "date_joined": s.date_joined,
            "batches": [{"id": b.id, "name": b.name} for b in s.student_batches.all()],
            "assigned_exams": _assigned_exams(s),
            "performance": stats.student_performance(s),
        })


class StudentAssignTests(APIView):
    """Assign (or unassign) individual tests to one student."""

    permission_classes = [IsAdminOrSuperAdmin]

    def post(self, request, pk):
        s = _student(pk)
        mode = request.data.get("mode", "add")
        if mode not in ("add", "remove"):
            raise serializers.ValidationError({"mode": "Use add or remove."})
        ids = request.data.get("exam_ids") or []
        exams = list(Exam.objects.filter(pk__in=ids).exclude(status=Exam.Status.ARCHIVED))
        if not exams:
            raise serializers.ValidationError({"exam_ids": "Choose at least one test that is not archived."})
        notes = []
        for e in exams:
            if mode == "add":
                e.assigned_students.add(s)
                if e.access_type != Exam.Access.ASSIGNED:
                    notes.append(f"'{e.title}' is not set to 'Assigned students', so this has no effect until its access is changed.")
            else:
                e.assigned_students.remove(s)
        log_activity(request.user, f"student.{mode}_tests", s, f"{len(exams)} tests")
        return Response({"assigned_exams": _assigned_exams(s), "notes": notes})


class StudentBatches(APIView):
    permission_classes = [IsAdminOrSuperAdmin]

    def put(self, request, pk):
        s = _student(pk)
        ids = request.data.get("batch_ids")
        if not isinstance(ids, list):
            raise serializers.ValidationError({"batch_ids": "Send a list of batch ids."})
        s.student_batches.set(Batch.objects.filter(pk__in=ids))
        log_activity(request.user, "student.set_batches", s, f"{len(ids)} batches")
        return Response({"batches": [{"id": b.id, "name": b.name} for b in s.student_batches.all()]})


def _pdf_response(data: bytes, filename: str) -> HttpResponse:
    resp = HttpResponse(data, content_type="application/pdf")
    resp["Content-Disposition"] = f'attachment; filename="{filename}"'
    return resp


class StudentReportPDF(APIView):
    permission_classes = [IsAdminOrSuperAdmin]

    def get(self, request, pk):
        s = _student(pk)
        perf = stats.student_performance(s)
        log_activity(request.user, "report.student_pdf", s)
        data = pdf.student_report(
            s, perf, [b.name for b in s.student_batches.all()], request.user,
        )
        return _pdf_response(data, f"student-report-{s.username}.pdf")


class ConsolidatedReportPDF(APIView):
    """One PDF for the chosen students, or for the chosen tests."""

    permission_classes = [IsAdminOrSuperAdmin]

    def post(self, request):
        student_ids = request.data.get("student_ids") or []
        exam_ids = request.data.get("exam_ids") or []
        if bool(student_ids) == bool(exam_ids):
            raise serializers.ValidationError({"detail": "Choose either students or tests, not both and not neither."})
        if student_ids:
            if len(student_ids) > MAX_REPORT_STUDENTS:
                raise serializers.ValidationError({"student_ids": f"Select at most {MAX_REPORT_STUDENTS} students."})
            students = list(User.objects.filter(pk__in=student_ids, role=Role.STUDENT).order_by("first_name", "username"))
            if not students:
                raise serializers.ValidationError({"student_ids": "No matching students."})
            data = pdf.consolidated_students([(s, stats.student_performance(s)) for s in students], request.user)
            log_activity(request.user, "report.consolidated_students_pdf", None, f"{len(students)} students")
            return _pdf_response(data, "consolidated-student-report.pdf")
        if len(exam_ids) > MAX_REPORT_EXAMS:
            raise serializers.ValidationError({"exam_ids": f"Select at most {MAX_REPORT_EXAMS} tests."})
        exams = list(Exam.objects.filter(pk__in=exam_ids).order_by("title"))
        if not exams:
            raise serializers.ValidationError({"exam_ids": "No matching tests."})
        data = pdf.consolidated_exams([(e, stats.exam_performance(e)) for e in exams], request.user)
        log_activity(request.user, "report.consolidated_tests_pdf", None, f"{len(exams)} tests")
        return _pdf_response(data, "consolidated-test-report.pdf")


class GlobalSearch(APIView):
    """Quick lookup across questions, tests and students for the top bar."""

    permission_classes = [IsAdminOrSuperAdmin]

    def get(self, request):
        from questionbank.models import Question

        q = (request.query_params.get("q") or "").strip()
        if len(q) < 2:
            return Response({"questions": [], "tests": [], "students": []})
        questions = Question.objects.filter(text__icontains=q).select_related("subject").order_by("-created_at")[:5]
        tests = Exam.objects.filter(Q(title__icontains=q) | Q(description__icontains=q)).order_by("-created_at")[:5]
        students = User.objects.filter(role=Role.STUDENT).filter(
            Q(username__icontains=q) | Q(email__icontains=q) | Q(first_name__icontains=q) | Q(last_name__icontains=q)
        ).order_by("first_name", "username")[:5]
        return Response({
            "questions": [{"id": x.id, "text": x.text[:90], "subject": x.subject.name} for x in questions],
            "tests": [{"id": x.id, "title": x.title, "status": x.status} for x in tests],
            "students": [{"id": x.id, "name": stats.name_of(x), "email": x.email} for x in students],
        })


class AdminDashboard(APIView):
    permission_classes = [IsAdminOrSuperAdmin]

    def get(self, request):
        return Response(stats.dashboard())
