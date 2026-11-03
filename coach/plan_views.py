import datetime

from django.db import transaction
from django.http import Http404
from django.shortcuts import get_object_or_404
from django.utils import timezone
from rest_framework import serializers
from rest_framework.response import Response
from rest_framework.views import APIView

from accounts.permissions import IsStudent
from exams.models import ExamCategory

from . import plan as engine
from .models import StudyPlan, StudyTask


class StudyPlanView(APIView):
    """
    GET: the student's plan (or {"plan": null}). POST {exam_date, target_name?, category?, daily_minutes?}: make a new plan, replacing
    the old one. DELETE: drop the plan.
    """

    permission_classes = [IsStudent]

    def get(self, request):
        plan = StudyPlan.objects.filter(student=request.user).select_related("category").first()
        if plan is None:
            return Response({"plan": None, "today": [], "overdue": [], "upcoming": []})
        return Response(engine.payload(plan))

    def post(self, request):
        today = timezone.localdate()
        try:
            exam_date = datetime.date.fromisoformat(str(request.data.get("exam_date", "")))
        except ValueError:
            raise serializers.ValidationError({"exam_date": "Choose the date of your exam."})
        if exam_date <= today:
            raise serializers.ValidationError({"exam_date": "The exam date must be after today."})
        if (exam_date - today).days > engine.MAX_DAYS:
            raise serializers.ValidationError({"exam_date": f"Plans can cover up to {engine.MAX_DAYS} days. Choose a nearer date."})
        try:
            minutes = int(request.data.get("daily_minutes", 60))
        except (TypeError, ValueError):
            raise serializers.ValidationError({"daily_minutes": "Give the minutes you can study each day."})
        if not engine.MIN_MINUTES <= minutes <= engine.MAX_MINUTES:
            raise serializers.ValidationError({"daily_minutes": f"Choose between {engine.MIN_MINUTES} and {engine.MAX_MINUTES} minutes."})
        category = None
        if request.data.get("category") not in (None, "", 0):
            # The exam lists give the public code of an exam; a plain number is still accepted.
            raw = str(request.data.get("category"))
            category = ExamCategory.objects.filter(public_id=raw).first() or (ExamCategory.objects.filter(pk=int(raw)).first() if raw.isdigit() else None)
            if category is None:
                raise Http404("That exam does not exist.")
        name = str(request.data.get("target_name") or (category.name if category else "My exam")).strip()[:120]
        if not name:
            raise serializers.ValidationError({"target_name": "Give your exam a name."})
        with transaction.atomic():
            StudyPlan.objects.filter(student=request.user).delete()
            plan = StudyPlan.objects.create(student=request.user, target_name=name, category=category, exam_date=exam_date, daily_minutes=minutes)
            engine.build_tasks(plan, request.user, today)
        return Response(engine.payload(plan), status=201)

    def delete(self, request):
        StudyPlan.objects.filter(student=request.user).delete()
        return Response(status=204)


class StudyTaskDone(APIView):
    """POST {done: true|false}: tick a task off, or undo it."""

    permission_classes = [IsStudent]

    def post(self, request, pk):
        task = get_object_or_404(StudyTask, pk=pk, plan__student=request.user)
        done = bool(request.data.get("done", True))
        task.done, task.done_at = done, (timezone.now() if done else None)
        task.save(update_fields=["done", "done_at"])
        return Response(engine.payload(task.plan))


class StudyPlanRefresh(APIView):
    """POST: plan the coming days again with the student's latest results (see engine.refresh)."""

    permission_classes = [IsStudent]

    def post(self, request):
        plan = get_object_or_404(StudyPlan, student=request.user)
        result = engine.refresh(plan)
        return Response({**result, **engine.payload(plan)})


class StudyPlanReschedule(APIView):
    """POST: catch up after missed days (see engine.reschedule)."""

    permission_classes = [IsStudent]

    def post(self, request):
        plan = get_object_or_404(StudyPlan, student=request.user)
        result = engine.reschedule(plan)
        return Response({**result, **engine.payload(plan)})
