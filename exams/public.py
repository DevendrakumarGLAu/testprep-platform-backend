from django.db.models import Count, Q
from django.shortcuts import get_object_or_404
from rest_framework.permissions import AllowAny
from rest_framework.response import Response
from rest_framework.views import APIView

from accounts.models import Role, User

from .models import Exam, ExamCategory, ExamQuestion


class PublicHome(APIView):
    """
    What the public landing page shows: the exams on offer and a few totals. Only names and counts, nothing a visitor
    could use to reach a test. Tests that are drafts, archived, or limited to chosen students are not counted.
    """

    permission_classes = [AllowAny]
    authentication_classes = []
    throttle_scope = "anon"

    def get(self, request):
        open_tests = Exam.objects.filter(status=Exam.Status.PUBLISHED, access_type=Exam.Access.PUBLIC, is_practice=False)
        categories = (
            ExamCategory.objects.filter(is_active=True)
            .annotate(tests=Count("exams", filter=Q(exams__in=open_tests), distinct=True))
            .filter(tests__gt=0)
            .prefetch_related("subjects")
            .order_by("-tests", "name")[:12]
        )
        data = {
            "stats": {
                "tests": open_tests.count(),
                "questions": ExamQuestion.objects.filter(section__exam__in=open_tests, question__is_active=True).values("question").distinct().count(),
                "students": User.objects.filter(role=Role.STUDENT, is_active=True).count(),
            },
            "exams": [
                {
                    "id": c.public_id,
                    "name": c.name,
                    "description": c.description,
                    "tests": c.tests,
                    "subjects": sorted(s.name for s in c.subjects.all() if s.is_active),
                }
                for c in categories
            ],
        }
        response = Response(data)
        response["Cache-Control"] = "public, max-age=300"
        return response


class PublicExamPage(APIView):
    """One exam on the public site: what it covers and the open tests in it. Names and counts only; nothing that opens a test."""

    permission_classes = [AllowAny]
    authentication_classes = []
    throttle_scope = "anon"

    def get(self, request, pid):
        category = get_object_or_404(ExamCategory.objects.prefetch_related("subjects"), public_id=pid, is_active=True)
        tests = (
            Exam.objects.filter(category=category, status=Exam.Status.PUBLISHED, access_type=Exam.Access.PUBLIC, is_practice=False)
            .with_totals().order_by("test_type", "title")
        )
        response = Response({
            "id": category.public_id, "name": category.name, "description": category.description,
            "subjects": sorted(s.name for s in category.subjects.all() if s.is_active),
            "tests": [
                {"title": e.title, "type": e.get_test_type_display(), "questions": e.question_count, "minutes": e.duration_minutes,
                 "premium": e.is_premium, "year": e.year}
                for e in tests
            ],
        })
        response["Cache-Control"] = "public, max-age=300"
        return response
