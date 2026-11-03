"""The feedback button: anyone signed in can send a note, and staff read and tick them off."""
from django.shortcuts import get_object_or_404
from rest_framework import serializers
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from accounts.models import Role, User
from accounts.permissions import IsAdminOrSuperAdmin
from notifications.models import Notification
from notifications.services import notify

from .models import Feedback

MESSAGE_MIN, MESSAGE_MAX = 5, 2000
DAILY_LIMIT = 20  # notes from one person in a day, so the inbox cannot be flooded


def row(f: Feedback) -> dict:
    who = f.user
    return {
        "id": f.id, "kind": f.kind, "kind_label": f.get_kind_display(), "message": f.message, "page": f.page, "device": f.device,
        "status": f.status, "created_at": f.created_at,
        "from_name": ((who.first_name + " " + who.last_name).strip() or who.username) if who else "Someone who has left",
        "from_email": who.email if who else "", "from_role": who.role if who else "",
    }


class FeedbackView(APIView):
    """POST {kind?, message, page?}: send a note (anyone signed in). GET: the inbox (staff), newest first; ?status=new for unread."""

    throttle_scope = "feedback"

    def get_permissions(self):
        return [IsAuthenticated()] if self.request.method == "POST" else [IsAdminOrSuperAdmin()]

    def get(self, request):
        qs = Feedback.objects.select_related("user")
        if request.query_params.get("status") in ("new", "done"):
            qs = qs.filter(status=request.query_params["status"])
        return Response({"results": [row(f) for f in qs[:200]], "new_count": Feedback.objects.filter(status=Feedback.Status.NEW).count()})

    def post(self, request):
        message = str(request.data.get("message") or "").strip()
        if len(message) < MESSAGE_MIN:
            raise serializers.ValidationError({"message": f"Please write at least {MESSAGE_MIN} characters."})
        if len(message) > MESSAGE_MAX:
            raise serializers.ValidationError({"message": f"Keep it under {MESSAGE_MAX} characters."})
        kind = request.data.get("kind")
        if kind not in Feedback.Kind.values:
            kind = Feedback.Kind.OTHER
        from django.utils import timezone

        since = timezone.now() - timezone.timedelta(days=1)
        if Feedback.objects.filter(user=request.user, created_at__gte=since).count() >= DAILY_LIMIT:
            raise serializers.ValidationError({"message": "You have sent a lot of notes today. Please try again tomorrow."})
        item = Feedback.objects.create(
            user=request.user, kind=kind, message=message, page=str(request.data.get("page") or "")[:300],
            device=request.META.get("HTTP_USER_AGENT", "")[:300],
        )
        staff = User.objects.filter(role__in=[Role.ADMIN, Role.SUPER_ADMIN], is_active=True)
        notify(staff, Notification.Kind.REPORT, f"New feedback: {item.get_kind_display()}", message[:160], "/admin/feedback")
        return Response({"detail": "Thank you. Your note reached us."}, status=201)


class FeedbackStatus(APIView):
    """POST {status: "new"|"done"}: tick a note off, or reopen it."""

    permission_classes = [IsAdminOrSuperAdmin]

    def post(self, request, pk):
        item = get_object_or_404(Feedback, pk=pk)
        status = request.data.get("status")
        if status not in Feedback.Status.values:
            raise serializers.ValidationError({"status": "Use new or done."})
        item.status = status
        item.save(update_fields=["status"])
        return Response(row(item))
