"""A morning note for each student with work left in today's plan."""
from django.utils import timezone

from notifications.models import Notification
from notifications.services import notify

from .models import StudyPlan, StudyTask


def send_plan_reminders(today=None) -> int:
    today = today or timezone.localdate()
    sent = 0
    for plan in StudyPlan.objects.filter(exam_date__gte=today).select_related("student"):
        todo = plan.tasks.filter(date=today, done=False).exclude(kind=StudyTask.Kind.EXAM_DAY)
        count = todo.count()
        if not count:
            continue
        days_left = (plan.exam_date - today).days
        minutes = sum(t.minutes for t in todo)
        sent += notify([plan.student], Notification.Kind.REMINDER, f"Today's study plan: {count} task{'s' if count != 1 else ''}",
                       f"About {minutes} minutes. {days_left} day{'s' if days_left != 1 else ''} to {plan.target_name}.",
                       "/student/plan", ref=f"plan:{today.isoformat()}")
    return sent
