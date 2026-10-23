"""A student's saved questions (with private notes) and the problems they report on questions."""
from django.db import IntegrityError, transaction
from django.shortcuts import get_object_or_404
from rest_framework import serializers
from rest_framework.response import Response
from rest_framework.views import APIView

from accounts.permissions import IsStudent
from questionbank.models import QuestionReport

from . import services as svc
from .models import Attempt, AttemptQuestion, SavedQuestion
from .views import own_attempt

NOTE_MAX = 1000


def _review_question(request, pid, number) -> tuple[Attempt, AttemptQuestion]:
    """The question as the student saw it, only once the test is submitted and its solutions are open to them."""
    attempt = own_attempt(request, pid)
    if attempt.status != Attempt.Status.SUBMITTED or not svc.solutions_visible(attempt.exam):
        raise serializers.ValidationError({"detail": "You can do this from the result review, once solutions are available."})
    aq = get_object_or_404(AttemptQuestion, attempt=attempt, order=number - 1)
    return attempt, aq


def _clean_text(request, field, required=False, minimum=0):
    value = str(request.data.get(field) or "").strip()
    if len(value) > NOTE_MAX:
        raise serializers.ValidationError({field: f"Keep it under {NOTE_MAX} characters."})
    if required and len(value) < minimum:
        raise serializers.ValidationError({field: f"Please write at least {minimum} characters."})
    return value


def saved_dict(s: SavedQuestion) -> dict:
    return {
        "id": s.id, "exam_title": s.exam_title, "subject": s.subject_name, "text": s.text, "text_hi": s.text_hi,
        "options": s.options, "selected_option": s.selected_option_id, "explanation": s.explanation,
        "explanation_hi": s.explanation_hi, "note": s.note, "created_at": s.created_at,
    }


class SaveQuestion(APIView):
    """POST {note?}: save (or update the note of) question `number` of this attempt. DELETE: remove it."""

    permission_classes = [IsStudent]

    def post(self, request, pid, number):
        attempt, aq = _review_question(request, pid, number)
        note = _clean_text(request, "note")
        with transaction.atomic():
            saved, created = SavedQuestion.objects.get_or_create(
                student=request.user, attempt_question=aq,
                defaults=dict(
                    question=aq.question, exam_title=attempt.exam.title, subject_name=aq.subject_name, text=aq.text, text_hi=aq.text_hi,
                    options=aq.options, selected_option_id=aq.selected_option_id, explanation=aq.explanation,
                    explanation_hi=aq.explanation_hi, note=note,
                ),
            )
            if not created and "note" in request.data:
                saved.note = note
                saved.save(update_fields=["note"])
        return Response(saved_dict(saved), status=201 if created else 200)

    def delete(self, request, pid, number):
        attempt = own_attempt(request, pid)
        SavedQuestion.objects.filter(student=request.user, attempt_question__attempt=attempt, attempt_question__order=number - 1).delete()
        return Response(status=204)


class SavedList(APIView):
    permission_classes = [IsStudent]

    def get(self, request):
        qs = SavedQuestion.objects.filter(student=request.user)
        if q := (request.query_params.get("subject") or "").strip():
            qs = qs.filter(subject_name__iexact=q)
        rows = list(qs)
        subjects = sorted({s.subject_name for s in SavedQuestion.objects.filter(student=request.user) if s.subject_name})
        return Response({"count": len(rows), "subjects": subjects, "results": [saved_dict(s) for s in rows]})


class SavedDetail(APIView):
    permission_classes = [IsStudent]

    def patch(self, request, pk):
        saved = get_object_or_404(SavedQuestion, pk=pk, student=request.user)
        saved.note = _clean_text(request, "note")
        saved.save(update_fields=["note"])
        return Response(saved_dict(saved))

    def delete(self, request, pk):
        get_object_or_404(SavedQuestion, pk=pk, student=request.user).delete()
        return Response(status=204)


class ReportQuestion(APIView):
    """POST {reason, message}: tell the admins that question `number` of this attempt is wrong."""

    permission_classes = [IsStudent]

    def post(self, request, pid, number):
        attempt, aq = _review_question(request, pid, number)
        if aq.question is None:
            raise serializers.ValidationError({"detail": "This question no longer exists in the question bank."})
        reason = request.data.get("reason")
        if reason not in QuestionReport.Reason.values:
            raise serializers.ValidationError({"reason": "Choose what is wrong."})
        message = _clean_text(request, "message", required=reason == "other", minimum=5)
        with transaction.atomic():
            try:
                report, created = QuestionReport.objects.update_or_create(
                    question=aq.question, student=request.user, status=QuestionReport.Status.OPEN,
                    defaults={"reason": reason, "message": message, "attempt_question": aq, "exam_title": attempt.exam.title},
                )
            except IntegrityError:  # two quick clicks: the other request created it first
                report = QuestionReport.objects.get(question=aq.question, student=request.user, status=QuestionReport.Status.OPEN)
                created = False
            q = aq.question
            if q.review_status != "needs_review":
                q.review_status = "needs_review"  # keep it out of newly published tests until an admin has looked
                q.save(update_fields=["review_status", "updated_at"])
        return Response({"id": report.id, "status": report.status, "updated": not created}, status=201 if created else 200)


class MyReports(APIView):
    """GET: the problems this student reported, with the admin's answer once there is one."""

    permission_classes = [IsStudent]

    def get(self, request):
        rows = QuestionReport.objects.filter(student=request.user).select_related("question")
        return Response({"results": [
            {"id": r.id, "question": r.question.display_text[:160], "exam_title": r.exam_title, "reason": r.get_reason_display(),
             "message": r.message, "status": r.status, "status_label": r.get_status_display(), "admin_note": r.admin_note,
             "created_at": r.created_at, "resolved_at": r.resolved_at}
            for r in rows
        ]})


def annotate_result(attempt, data) -> None:
    """Adds `saved_id` and `report_status` to each question of a student's own result, so the review can show the buttons' state."""
    saved = dict(SavedQuestion.objects.filter(student=attempt.student, attempt_question__attempt=attempt)
                 .values_list("attempt_question__order", "id"))
    reports = dict(QuestionReport.objects.filter(student=attempt.student, attempt_question__attempt=attempt)
                   .values_list("attempt_question__order", "status"))
    for item in data["questions"]:
        item["saved_id"] = saved.get(item["number"] - 1)
        item["report_status"] = reports.get(item["number"] - 1)
