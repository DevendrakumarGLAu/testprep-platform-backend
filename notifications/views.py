from django.db.models import Count, Q
from django.shortcuts import get_object_or_404
from django.utils import timezone
from rest_framework import serializers
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from accounts.models import log_activity
from accounts.permissions import IsAdminOrSuperAdmin
from exams.models import Batch, ExamCategory

from . import services
from .models import Notification


def row(n: Notification) -> dict:
    return {"id": n.id, "kind": n.kind, "title": n.title, "body": n.body, "link": n.link, "created_at": n.created_at, "read": n.read_at is not None}


class NotificationList(APIView):
    """GET: my latest notifications (30) and how many are unread. ?unread=1 shows only the unread ones."""

    permission_classes = [IsAuthenticated]
    LIMIT = 30

    def get(self, request):
        mine = Notification.objects.filter(user=request.user)
        unread = mine.filter(read_at__isnull=True).count()
        shown = mine.filter(read_at__isnull=True) if request.query_params.get("unread") in ("1", "true") else mine
        return Response({"unread": unread, "results": [row(n) for n in shown[: self.LIMIT]]})


class UnreadCount(APIView):
    """GET: just the unread count, cheap enough for the top bar to ask every minute."""

    permission_classes = [IsAuthenticated]

    def get(self, request):
        return Response({"unread": Notification.objects.filter(user=request.user, read_at__isnull=True).count()})


class MarkRead(APIView):
    """POST {ids: [...]} or {all: true}: mark my notifications as read."""

    permission_classes = [IsAuthenticated]

    def post(self, request):
        mine = Notification.objects.filter(user=request.user, read_at__isnull=True)
        if request.data.get("all") is True:
            n = mine.update(read_at=timezone.now())
        else:
            ids = request.data.get("ids")
            if not isinstance(ids, list) or not all(isinstance(i, int) for i in ids):
                raise serializers.ValidationError({"ids": "Send a list of notification ids, or all: true."})
            n = mine.filter(pk__in=ids).update(read_at=timezone.now())
        return Response({"marked": n})


class Announce(APIView):
    """POST: an admin sends a message to all students, the students preparing for an exam, or one batch."""

    permission_classes = [IsAdminOrSuperAdmin]

    def post(self, request):
        title = str(request.data.get("title") or "").strip()
        body = str(request.data.get("body") or "").strip()
        kind = request.data.get("audience", "all")
        target = request.data.get("target")
        if not title or len(title) > 200:
            raise serializers.ValidationError({"title": "Give the announcement a title (up to 200 characters)."})
        if len(body) > 1000:
            raise serializers.ValidationError({"body": "Keep the message under 1000 characters."})
        if kind not in ("all", "exam", "batch"):
            raise serializers.ValidationError({"audience": "Choose all students, an exam or a batch."})
        if kind == "exam":
            get_object_or_404(ExamCategory, pk=target)
        if kind == "batch":
            get_object_or_404(Batch, pk=target)
        recipients = services.audience(kind, target)
        link = str(request.data.get("link") or "").strip()
        if link and not link.startswith("/"):
            raise serializers.ValidationError({"link": "Use a page of this app, such as /student/tests."})
        sent = services.notify(recipients, Notification.Kind.ANNOUNCEMENT, title, body, link)
        log_activity(request.user, "announcement.send", None, f"{title[:60]} to {sent} students")
        return Response({"sent": sent}, status=201)
