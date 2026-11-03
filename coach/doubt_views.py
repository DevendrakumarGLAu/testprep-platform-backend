"""Doubts: a student asks about one question of a result review; teachers answer in a thread."""
from django.db.models import Count, Q
from django.shortcuts import get_object_or_404
from rest_framework import serializers
from rest_framework.response import Response
from rest_framework.views import APIView

from accounts.models import Role, User, log_activity
from accounts.permissions import IsAdminOrSuperAdmin, IsStudent
from attempts.saved_views import _review_question
from notifications.models import Notification
from notifications.services import notify

from . import ai
from .models import Doubt, DoubtReply

MESSAGE_MAX = 1500
MESSAGE_MIN = 5
OPEN_LIMIT = 10  # a student can have this many unanswered doubts at a time, so the inbox stays useful


def _text(request, field="message") -> str:
    value = str(request.data.get(field) or "").strip()
    if len(value) < MESSAGE_MIN:
        raise serializers.ValidationError({field: f"Please write at least {MESSAGE_MIN} characters."})
    if len(value) > MESSAGE_MAX:
        raise serializers.ValidationError({field: f"Keep it under {MESSAGE_MAX} characters."})
    return value


def _staff():
    return User.objects.filter(role__in=[Role.ADMIN, Role.SUPER_ADMIN], is_active=True)


def reply_dict(r: DoubtReply) -> dict:
    return {"id": r.id, "role": r.role, "author": (r.author.first_name or r.author.username) if r.author_id else "", "body": r.body,
            "via_ai": r.via_ai, "created_at": r.created_at}


def doubt_summary(d: Doubt) -> dict:
    return {"id": d.id, "status": d.status, "exam_title": d.exam_title, "question_text": d.question_text[:160], "created_at": d.created_at,
            "updated_at": d.updated_at, "reply_count": getattr(d, "reply_count", None)}


def doubt_detail(d: Doubt, staff=False) -> dict:
    out = {
        **doubt_summary(d), "question_text": d.question_text, "question_text_hi": d.question_text_hi, "options": d.options,
        "explanation": d.explanation, "explanation_hi": d.explanation_hi, "selected_option_text": d.selected_option_text,
        "replies": [reply_dict(r) for r in d.replies.select_related("author")],
    }
    if staff:
        out["student"] = d.student.get_full_name() or d.student.username
        out["ai_available"] = ai.configured()
    return out


class AskDoubt(APIView):
    """POST {message}: ask about question `number` of a reviewed attempt. Asking again about the same open doubt adds to its thread."""

    permission_classes = [IsStudent]

    def post(self, request, pid, number):
        attempt, aq = _review_question(request, pid, number)
        message = _text(request)
        doubt = Doubt.objects.filter(student=request.user, attempt_question=aq).exclude(status=Doubt.Status.RESOLVED).first()
        created = doubt is None
        if created:
            if Doubt.objects.filter(student=request.user, status=Doubt.Status.OPEN).count() >= OPEN_LIMIT:
                raise serializers.ValidationError({"detail": f"You already have {OPEN_LIMIT} doubts waiting for an answer. Please wait for a reply."})
            chosen = next((o.get("text", "") for o in aq.options if o.get("id") == aq.selected_option_id), "")
            doubt = Doubt.objects.create(
                student=request.user, question=aq.question, attempt_question=aq, exam_title=attempt.exam.title, question_text=aq.text,
                question_text_hi=aq.text_hi, explanation=aq.explanation, explanation_hi=aq.explanation_hi, selected_option_text=chosen[:500],
                options=[{"text": o.get("text", ""), "text_hi": o.get("text_hi", ""), "is_correct": bool(o.get("is_correct"))} for o in aq.options],
            )
        DoubtReply.objects.create(doubt=doubt, author=request.user, role=DoubtReply.Role.STUDENT, body=message)
        doubt.status = Doubt.Status.OPEN
        doubt.save()  # moves it to the top of the teachers' list
        notify(_staff(), Notification.Kind.DOUBT, "New doubt" if created else "A student added to a doubt",
               f"{request.user.get_full_name() or request.user.username}: {message[:120]}", "/admin/doubts",
               ref=f"doubt:{doubt.id}:{doubt.replies.count()}")
        return Response(doubt_detail(doubt), status=201 if created else 200)


class MyDoubts(APIView):
    permission_classes = [IsStudent]

    def get(self, request):
        rows = Doubt.objects.filter(student=request.user).annotate(reply_count=Count("replies"))
        status = request.query_params.get("status")
        if status in Doubt.Status.values:
            rows = rows.filter(status=status)
        return Response([doubt_summary(d) for d in rows[:100]])


class MyDoubtDetail(APIView):
    permission_classes = [IsStudent]

    def get(self, request, pk):
        return Response(doubt_detail(get_object_or_404(Doubt, pk=pk, student=request.user)))


class MyDoubtReply(APIView):
    """POST {body}: add to the conversation. The doubt goes back to 'waiting for an answer'."""

    permission_classes = [IsStudent]

    def post(self, request, pk):
        doubt = get_object_or_404(Doubt, pk=pk, student=request.user)
        body = _text(request, "body")
        DoubtReply.objects.create(doubt=doubt, author=request.user, role=DoubtReply.Role.STUDENT, body=body)
        doubt.status = Doubt.Status.OPEN
        doubt.save()
        notify(_staff(), Notification.Kind.DOUBT, "A student added to a doubt",
               f"{request.user.get_full_name() or request.user.username}: {body[:120]}", "/admin/doubts",
               ref=f"doubt:{doubt.id}:{doubt.replies.count()}")
        return Response(doubt_detail(doubt))


class MyDoubtResolve(APIView):
    """POST: the student says the doubt is cleared."""

    permission_classes = [IsStudent]

    def post(self, request, pk):
        doubt = get_object_or_404(Doubt, pk=pk, student=request.user)
        doubt.status = Doubt.Status.RESOLVED
        doubt.save()
        return Response(doubt_detail(doubt))


# ---- teachers -------------------------------------------------------------------------------

class AdminDoubts(APIView):
    """GET ?status=open|answered|resolved&search=: the inbox, newest activity first, with the count of each status."""

    permission_classes = [IsAdminOrSuperAdmin]

    def get(self, request):
        base = Doubt.objects.select_related("student").annotate(reply_count=Count("replies"))
        counts = {s: Doubt.objects.filter(status=s).count() for s in Doubt.Status.values}
        status = request.query_params.get("status")
        if status in Doubt.Status.values:
            base = base.filter(status=status)
        search = (request.query_params.get("search") or "").strip()
        if search:
            base = base.filter(Q(question_text__icontains=search) | Q(exam_title__icontains=search) | Q(student__username__icontains=search)
                               | Q(student__first_name__icontains=search))
        rows = [{**doubt_summary(d), "student": d.student.get_full_name() or d.student.username} for d in base[:200]]
        return Response({"counts": counts, "results": rows})


class AdminDoubtDetail(APIView):
    permission_classes = [IsAdminOrSuperAdmin]

    def get(self, request, pk):
        return Response(doubt_detail(get_object_or_404(Doubt.objects.select_related("student"), pk=pk), staff=True))


class AdminDoubtReply(APIView):
    """POST {body, via_ai?}: answer the student. They get a message in the bell."""

    permission_classes = [IsAdminOrSuperAdmin]

    def post(self, request, pk):
        doubt = get_object_or_404(Doubt.objects.select_related("student"), pk=pk)
        body = _text(request, "body")
        DoubtReply.objects.create(doubt=doubt, author=request.user, role=DoubtReply.Role.STAFF, body=body, via_ai=bool(request.data.get("via_ai")))
        doubt.status = Doubt.Status.ANSWERED
        doubt.save()
        notify([doubt.student], Notification.Kind.DOUBT, "Your doubt has an answer", body[:140], f"/student/doubts/{doubt.id}",
               ref=f"doubt-reply:{doubt.id}:{doubt.replies.count()}")
        log_activity(request.user, "doubt_replied", doubt)
        return Response(doubt_detail(doubt, staff=True))


class AdminDoubtStatus(APIView):
    """POST {status}: mark a doubt open, answered or resolved without writing a reply."""

    permission_classes = [IsAdminOrSuperAdmin]

    def post(self, request, pk):
        doubt = get_object_or_404(Doubt.objects.select_related("student"), pk=pk)
        status = request.data.get("status")
        if status not in Doubt.Status.values:
            raise serializers.ValidationError({"status": "Choose open, answered or resolved."})
        doubt.status = status
        doubt.save()
        return Response(doubt_detail(doubt, staff=True))


class AdminDoubtAISuggest(APIView):
    """POST: a draft answer from the AI helper. Nothing is sent to the student; the teacher edits it and sends it."""

    permission_classes = [IsAdminOrSuperAdmin]

    def post(self, request, pk):
        doubt = get_object_or_404(Doubt.objects.select_related("student"), pk=pk)
        try:
            text = ai.suggest(doubt)
        except ai.AIUnavailable as exc:
            return Response({"detail": str(exc)}, status=503)
        return Response({"suggestion": text})
