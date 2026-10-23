from django.shortcuts import get_object_or_404
from django.utils import timezone
from rest_framework import serializers
from rest_framework.response import Response
from rest_framework.views import APIView

from accounts.permissions import IsStudent

from . import practice, services as svc
from .models import Attempt
from .views import own_attempt


def _start(student, exam):
    """Starts (or resumes) the attempt on a practice set and returns the response the app navigates with."""
    try:
        attempt, created = svc.start_attempt(exam, student)
    except svc.CannotStart as e:
        raise practice.PracticeError(e.reason, 403)
    return Response({"attempt_id": attempt.public_id, "exam_id": exam.public_id, "questions": exam.sections.first().items.count(), "created": created})


class PracticeView(APIView):
    """Base class: turns PracticeError into a clear message."""

    permission_classes = [IsStudent]

    def handle_exception(self, exc):
        if isinstance(exc, practice.PracticeError):
            return Response({"detail": exc.message}, status=exc.status)
        return super().handle_exception(exc)


class WeakTopics(PracticeView):
    """GET: the topics this student is weakest in, with their accuracy."""

    def get(self, request):
        stats = practice.topic_stats(request.user)
        return Response({
            "weak": practice.weak_topics(request.user),
            "all": sorted(stats, key=lambda t: t["accuracy"]),
            "rule": f"A topic counts as weak with at least {practice.MIN_ANSWERED_FOR_WEAK} answers and under {practice.WEAK_BELOW_PERCENT}% correct.",
        })


class PracticeWeak(PracticeView):
    """POST {topics: [{topic_id, subject_id}], count}: a practice set from the chosen topics (the 3 weakest if none are given)."""

    def post(self, request):
        count = request.data.get("count", 15)
        if not isinstance(count, int) or not 1 <= count <= practice.MAX_PRACTICE_QUESTIONS:
            raise serializers.ValidationError({"count": f"Choose between 1 and {practice.MAX_PRACTICE_QUESTIONS} questions."})
        topics = request.data.get("topics") or [
            {"topic_id": t["topic_id"], "subject_id": t["subject_id"]} for t in practice.weak_topics(request.user, limit=3)
        ]
        if not topics:
            raise practice.PracticeError("Take a few tests first: we need at least 3 answers in a topic to know where you need practice.", 404)
        clean = []
        for t in topics:
            if not isinstance(t, dict) or not (t.get("topic_id") or t.get("subject_id")):
                raise serializers.ValidationError({"topics": "Each topic needs a topic_id or a subject_id."})
            clean.append({"topic_id": t.get("topic_id"), "subject_id": t.get("subject_id")})
        questions = practice.weak_topic_questions(request.user, clean, count)
        exam = practice.build_practice_exam(request.user, questions, f"Practice: weak topics ({timezone.localdate():%d %b})", "weak")
        return _start(request.user, exam)


class PracticeWrong(PracticeView):
    """POST: a practice set from the questions this attempt got wrong (and left unanswered, unless include_unanswered is false)."""

    def post(self, request, pid):
        attempt = own_attempt(request, pid)
        if attempt.status != Attempt.Status.SUBMITTED:
            raise practice.PracticeError("Submit the test first.", 409)
        if not svc.solutions_visible(attempt.exam):
            raise practice.PracticeError("Solutions for this test have not been released yet.", 403)
        include = request.data.get("include_unanswered", True) is not False
        questions = practice.wrong_answer_questions(attempt, include)
        if not questions:
            raise practice.PracticeError("Nothing to redo: you got everything right.", 404)
        exam = practice.build_practice_exam(request.user, questions, f"Practice: redo mistakes from {attempt.exam.title}"[:200], "wrong")
        return _start(request.user, exam)


class DailyQuiz(PracticeView):
    def get(self, request):
        exam = practice.todays_quiz(request.user)
        done = bool(exam and Attempt.objects.filter(exam=exam, student=request.user, status=Attempt.Status.SUBMITTED).exists())
        active = bool(exam and Attempt.objects.filter(exam=exam, student=request.user, status=Attempt.Status.IN_PROGRESS).exists())
        ready = bool(exam) or len(practice.daily_quiz_questions(request.user)) >= practice.DAILY_QUIZ_MIN
        return Response({"available": ready, "done": done, "in_progress": active, "size": practice.DAILY_QUIZ_SIZE})

    def post(self, request):
        today = timezone.localdate()
        exam = practice.todays_quiz(request.user, today)
        if exam is None:
            questions = practice.daily_quiz_questions(request.user, today)
            if len(questions) < practice.DAILY_QUIZ_MIN:
                raise practice.PracticeError("Today's quiz is not ready yet. Check back soon.", 404)
            exam = practice.build_practice_exam(request.user, questions, practice.daily_quiz_title(today), "daily", max_attempts=1)
        elif Attempt.objects.filter(exam=exam, student=request.user, status=Attempt.Status.SUBMITTED).exists():
            raise practice.PracticeError("You have already done today's quiz. A new one is ready tomorrow.", 409)
        return _start(request.user, exam)
