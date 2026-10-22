from decimal import Decimal

from django.db.models import Avg, Count, Max, Min, Q
from django.shortcuts import get_object_or_404
from rest_framework import generics, serializers
from rest_framework.response import Response
from rest_framework.views import APIView

from accounts.models import log_activity
from accounts.permissions import IsAdminOrSuperAdmin
from exams.models import Exam

from . import services as svc
from .models import Attempt, AttemptQuestion
from .results import build_result


def money(v):
    """Two-decimal string; SQLite aggregates come back unformatted."""
    return None if v is None else str(Decimal(str(v)).quantize(Decimal("0.01")))


class AdminAttemptSerializer(serializers.ModelSerializer):
    student_name = serializers.SerializerMethodField()
    student_email = serializers.CharField(source="student.email")
    percentage = serializers.SerializerMethodField()

    class Meta:
        model = Attempt
        fields = [
            "id", "student", "student_name", "student_email", "attempt_number", "status", "started_at",
            "submitted_at", "auto_submitted", "score", "total_marks", "percentage", "correct_count",
            "incorrect_count", "unanswered_count", "accuracy", "passed", "time_taken_seconds",
        ]

    def get_student_name(self, a):
        return (f"{a.student.first_name} {a.student.last_name}").strip() or a.student.username

    def get_percentage(self, a):
        if a.score is None or not a.total_marks:
            return None
        return round(float(a.score) / float(a.total_marks) * 100, 1)


class ExamAttempts(generics.ListAPIView):
    """All attempts on one test, for admins."""

    permission_classes = [IsAdminOrSuperAdmin]
    serializer_class = AdminAttemptSerializer
    filterset_fields = ["status", "passed", "student"]
    ordering_fields = ["score", "submitted_at", "started_at", "time_taken_seconds"]

    def get_queryset(self):
        exam = get_object_or_404(Exam, pk=self.kwargs["pk"])
        svc.expire_overdue(Attempt.objects.filter(exam=exam))
        qs = Attempt.objects.filter(exam=exam).select_related("student")
        if q := self.request.query_params.get("search"):
            qs = qs.filter(
                Q(student__username__icontains=q) | Q(student__email__icontains=q)
                | Q(student__first_name__icontains=q) | Q(student__last_name__icontains=q)
            )
        return qs.order_by("-score", "-submitted_at")


class AdminAttemptDetail(APIView):
    """Full review of any submitted attempt, regardless of release settings."""

    permission_classes = [IsAdminOrSuperAdmin]

    def get(self, request, pk):
        attempt = get_object_or_404(Attempt.objects.select_related("exam", "student"), pk=pk)
        if attempt.status != Attempt.Status.SUBMITTED:
            return Response({"detail": "This attempt is still in progress."}, status=409)
        return Response(build_result(attempt, request, reveal_solutions=True, include_student=True))


class ExamResultsSummary(APIView):
    permission_classes = [IsAdminOrSuperAdmin]

    def get(self, request, pk):
        exam = get_object_or_404(Exam, pk=pk)
        svc.expire_overdue(Attempt.objects.filter(exam=exam))
        done = Attempt.objects.filter(exam=exam, status=Attempt.Status.SUBMITTED)
        agg = done.aggregate(avg=Avg("score"), hi=Max("score"), lo=Min("score"), n=Count("id"))
        total = float(done.first().total_marks) if done.exists() else float(exam.total_marks)

        def pct(v):
            return round(float(v) / total * 100, 1) if v is not None and total else None

        passed = done.filter(passed=True).count()
        questions = []
        stats = (
            AttemptQuestion.objects.filter(attempt__in=done)
            .values("question", "text", "text_hi", "subject_name")
            .annotate(
                total=Count("id"),
                correct=Count("id", filter=Q(is_correct=True)),
                wrong=Count("id", filter=Q(is_correct=False)),
                avg_time=Avg("time_spent_seconds"),
            )
            .order_by("question")
        )
        for r in stats:
            answered = r["correct"] + r["wrong"]
            questions.append({
                "question": r["question"], "text": (r["text"] or r["text_hi"])[:160], "subject": r["subject_name"],
                "attempts": r["total"], "correct_percent": round(r["correct"] * 100 / r["total"], 1),
                "answered_percent": round(answered * 100 / r["total"], 1),
                "average_seconds": round(r["avg_time"] or 0),
            })
        questions.sort(key=lambda q: q["correct_percent"])
        return Response({
            "exam": {
                "id": exam.id, "title": exam.title, "result_release": exam.result_release,
                "results_released": exam.results_released, "solution_release": exam.solution_release,
                "solutions_released": exam.solutions_released, "result_release_at": exam.result_release_at,
                "results_visible": svc.result_visible(exam), "solutions_visible": svc.solutions_visible(exam),
            },
            "attempts_submitted": agg["n"],
            "attempts_in_progress": Attempt.objects.filter(exam=exam, status=Attempt.Status.IN_PROGRESS).count(),
            "students": done.values("student").distinct().count(),
            "average_score": money(agg["avg"]),
            "average_percentage": pct(agg["avg"]),
            "highest_score": money(agg["hi"]),
            "lowest_score": money(agg["lo"]),
            "pass_rate": round(passed * 100 / agg["n"], 1) if agg["n"] else None,
            "questions": questions,
        })


CONTROLS = {
    # action: (exam policy field, required policy value, flag to set, new flag value)
    "release-results": ("result_release", Exam.ResultRelease.MANUAL, "results_released", True),
    "hide-results": ("result_release", Exam.ResultRelease.MANUAL, "results_released", False),
    "release-solutions": ("solution_release", Exam.SolutionRelease.MANUAL, "solutions_released", True),
    "hide-solutions": ("solution_release", Exam.SolutionRelease.MANUAL, "solutions_released", False),
}


class ReleaseControl(APIView):
    """Manual release switches. They only apply when the test is set to manual release."""

    permission_classes = [IsAdminOrSuperAdmin]
    action_name = ""

    def post(self, request, pk):
        exam = get_object_or_404(Exam, pk=pk)
        policy_field, required, flag, value = CONTROLS[self.action_name]
        if getattr(exam, policy_field) != required:
            return Response(
                {"detail": f"This test does not use manual release. Change its {policy_field.replace('_', ' ')} setting first."},
                status=400,
            )
        setattr(exam, flag, value)
        exam.save(update_fields=[flag, "updated_at"])
        log_activity(request.user, f"exam.{self.action_name.replace('-', '_')}", exam)
        return Response({
            "results_released": exam.results_released, "solutions_released": exam.solutions_released,
            "results_visible": svc.result_visible(exam), "solutions_visible": svc.solutions_visible(exam),
        })
