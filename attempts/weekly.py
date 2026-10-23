"""A student's last seven days compared with the seven before: how much they practised and how well it went."""
import datetime
from collections import defaultdict

from django.utils import timezone

from . import services as svc
from .activity import questions_per_day
from .models import Attempt, AttemptQuestion

MIN_FOR_SUBJECT = 3  # answers in a subject before it can be called best or weakest


def _pct(correct, answered):
    return round(correct * 100 / answered, 1) if answered else None


def summary(student, today=None, now=None):
    now = now or timezone.now()
    today = today or timezone.localdate()
    this_start = today - datetime.timedelta(days=6)
    last_start = today - datetime.timedelta(days=13)
    per_day = questions_per_day(student, last_start)
    this_week = {d: n for d, n in per_day.items() if d >= this_start}
    last_week = {d: n for d, n in per_day.items() if d < this_start}

    # Accuracy only counts tests whose results have been released, so it never shows a score early.
    first_instant = timezone.make_aware(datetime.datetime.combine(last_start, datetime.time.min))
    attempts = [
        a for a in Attempt.objects.filter(student=student, status=Attempt.Status.SUBMITTED, submitted_at__gte=first_instant).select_related("exam")
        if svc.result_visible(a.exam, now)
    ]
    period = {a.id: ("this" if timezone.localtime(a.submitted_at).date() >= this_start else "last") for a in attempts}
    totals = {"this": [0, 0], "last": [0, 0]}  # answered, correct
    subjects = defaultdict(lambda: [0, 0])
    rows = AttemptQuestion.objects.filter(attempt__in=list(period), is_correct__isnull=False).values("attempt_id", "subject_name", "is_correct")
    for r in rows:
        p = period[r["attempt_id"]]
        totals[p][0] += 1
        totals[p][1] += 1 if r["is_correct"] else 0
        if p == "this":
            s = subjects[r["subject_name"] or "General"]
            s[0] += 1
            s[1] += 1 if r["is_correct"] else 0
    ranked = sorted(
        ((name, _pct(c, n), n) for name, (n, c) in subjects.items() if n >= MIN_FOR_SUBJECT), key=lambda x: (-x[1], -x[2], x[0]),
    )
    now_acc, before_acc = _pct(totals["this"][1], totals["this"][0]), _pct(totals["last"][1], totals["last"][0])
    answered, before = sum(this_week.values()), sum(last_week.values())
    return {
        "from": this_start.isoformat(),
        "to": today.isoformat(),
        "questions": answered,
        "questions_before": before,
        "tests": sum(1 for p in period.values() if p == "this"),
        "tests_before": sum(1 for p in period.values() if p == "last"),
        "active_days": len(this_week),
        "accuracy": now_acc,
        "accuracy_before": before_acc,
        "accuracy_change": round(now_acc - before_acc, 1) if now_acc is not None and before_acc is not None else None,
        "best_subject": {"name": ranked[0][0], "accuracy": ranked[0][1]} if ranked else None,
        "weakest_subject": {"name": ranked[-1][0], "accuracy": ranked[-1][1]} if len(ranked) > 1 else None,
    }


def has_activity(data) -> bool:
    return bool(data["questions"] or data["tests"])


def message(data):
    """(title, body) for the notification and the email."""
    lines = [f"You answered {data['questions']} question{'s' if data['questions'] != 1 else ''} on {data['active_days']} day{'s' if data['active_days'] != 1 else ''}"
             + (f" and took {data['tests']} test{'s' if data['tests'] != 1 else ''}." if data["tests"] else ".")]
    if data["questions_before"]:
        diff = data["questions"] - data["questions_before"]
        lines.append(f"That is {abs(diff)} {'more' if diff > 0 else 'fewer'} than the week before." if diff else "That is the same as the week before.")
    if data["accuracy"] is not None:
        lines.append(f"Accuracy in your tests: {data['accuracy']}%" + (f" ({data['accuracy_change']:+g} points)." if data["accuracy_change"] is not None else "."))
    if data["best_subject"]:
        lines.append(f"Strongest subject: {data['best_subject']['name']} ({data['best_subject']['accuracy']}%).")
    if data["weakest_subject"]:
        lines.append(f"Needs work: {data['weakest_subject']['name']} ({data['weakest_subject']['accuracy']}%).")
    return "Your week on the platform", " ".join(lines)
