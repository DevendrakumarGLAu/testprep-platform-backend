"""Pop-ups for students: who should see which one, and what is waiting for a student right now."""
from django.db.models import Q
from django.utils import timezone

from accounts.models import Role, User

from .models import Popup, PopupSeen


def audience_of(popup: Popup):
    """The active students this pop-up is meant for."""
    students = User.objects.filter(role=Role.STUDENT, is_active=True)
    if popup.audience == Popup.Audience.EXAM:
        return students.filter(preparations__category_id=popup.category_id).distinct()
    if popup.audience == Popup.Audience.BATCH:
        return students.filter(student_batches__id=popup.batch_id).distinct()
    return students


def state_of(popup: Popup, now=None) -> str:
    """off, scheduled, live or ended, for the admin list."""
    now = now or timezone.now()
    if not popup.is_active:
        return "off"
    if popup.ends_at and popup.ends_at <= now:
        return "ended"
    if popup.starts_at and popup.starts_at > now:
        return "scheduled"
    return "live"


def pending_for(student, now=None):
    """The newest live pop-up meant for this student that they have not closed yet, or None."""
    now = now or timezone.now()
    live = Popup.objects.filter(is_active=True).filter(Q(starts_at__isnull=True) | Q(starts_at__lte=now)).filter(Q(ends_at__isnull=True) | Q(ends_at__gt=now))
    live = live.exclude(seen__student=student)
    preparing = student.preparations.values("category_id")
    mine = Q(audience=Popup.Audience.ALL)
    mine |= Q(audience=Popup.Audience.EXAM, category_id__in=preparing)
    mine |= Q(audience=Popup.Audience.BATCH, batch_id__in=student.student_batches.values("id"))
    return live.filter(mine).order_by("-created_at", "-id").first()


def mark_seen(popup: Popup, student) -> None:
    PopupSeen.objects.get_or_create(popup=popup, student=student)
