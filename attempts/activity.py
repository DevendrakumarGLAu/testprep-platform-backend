"""A student's daily practice activity: how many questions they answered each day, their streak and their daily goal."""
import datetime

from django.db.models import Count
from django.db.models.functions import TruncDate
from django.utils import timezone

from .models import AttemptQuestion


def questions_per_day(student, since):
    rows = (
        AttemptQuestion.objects.filter(attempt__student=student, answered_at__isnull=False, selected_option_id__isnull=False)
        .annotate(d=TruncDate("answered_at", tzinfo=timezone.get_current_timezone()))
        .filter(d__gte=since)
        .values("d").annotate(n=Count("id"))
    )
    return {r["d"]: r["n"] for r in rows}


def streaks(days_with_activity, today):
    """(current, best): consecutive days with activity. The current streak survives until the day after a missed one."""
    days = sorted(days_with_activity)
    best = run = 0
    prev = None
    for d in days:
        run = run + 1 if prev is not None and (d - prev).days == 1 else 1
        best = max(best, run)
        prev = d
    have = set(days)
    cursor = today if today in have else today - datetime.timedelta(days=1)
    current = 0
    while cursor in have:
        current += 1
        cursor -= datetime.timedelta(days=1)
    return current, best


def summary(student, today=None):
    today = today or timezone.localdate()
    # one year of history is enough for the streak and the little chart
    per_day = questions_per_day(student, today - datetime.timedelta(days=365))
    current, best = streaks(per_day.keys(), today)
    goal = getattr(student, "daily_goal_questions", 20) or 20
    last7 = [today - datetime.timedelta(days=i) for i in range(6, -1, -1)]
    return {
        "streak": current,
        "best_streak": best,
        "today": per_day.get(today, 0),
        "goal": goal,
        "goal_reached": per_day.get(today, 0) >= goal,
        "last_7_days": [{"date": d.isoformat(), "questions": per_day.get(d, 0)} for d in last7],
    }
