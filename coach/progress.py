"""How each student in a batch is doing: who has fallen behind, who is improving, who is slipping."""
import csv
from datetime import timedelta
from io import StringIO

from django.utils import timezone

from attempts.models import Attempt

from .models import StudyTask

INACTIVE_DAYS = 7  # no test or practice for this long counts as behind
OVERDUE_BEHIND = 3  # this many missed tasks in the study plan counts as behind
TREND_POINTS = 3
TREND_DELTA = 5.0  # percentage points between the last three tests and the three before
SPARK_POINTS = 8
STATUS_ORDER = {"behind": 0, "declining": 1, "steady": 2, "improving": 3, "new": 4}


def _mean(values):
    return sum(values) / len(values) if values else None


def percent(a: Attempt):
    return float(a.score) / float(a.total_marks) * 100 if a.total_marks else None


def student_row(student, attempts, last_active, overdue, now, days):
    scored = [(a, percent(a)) for a in attempts if percent(a) is not None]
    scored.sort(key=lambda t: t[0].submitted_at)
    series = [round(p, 1) for _, p in scored]
    window_start = now - timedelta(days=days)
    in_window = [p for a, p in scored if a.submitted_at >= window_start]
    delta = None
    if len(series) > TREND_POINTS:
        recent = _mean(series[-TREND_POINTS:])
        before = _mean(series[-2 * TREND_POINTS:-TREND_POINTS])
        delta = round(recent - before, 1)
    inactive = (now - last_active).days if last_active else None
    reasons, status = [], "steady"
    if inactive is None:
        status, reasons = "new", ["Has not taken any test yet"]
    elif inactive >= INACTIVE_DAYS:
        status, reasons = "behind", [f"No activity for {inactive} days"]
    if overdue >= OVERDUE_BEHIND and status != "new":
        reasons.append(f"{overdue} study-plan tasks missed")
        status = "behind"
    if status == "steady" and delta is not None:
        if delta <= -TREND_DELTA:
            status, reasons = "declining", [f"Scores down {abs(delta)} points"]
        elif delta >= TREND_DELTA:
            status, reasons = "improving", [f"Scores up {delta} points"]
    return {
        "id": student.id, "name": student.get_full_name() or student.username, "username": student.username, "status": status,
        "reasons": reasons, "tests_taken": len(in_window), "average_percent": round(_mean(in_window), 1) if in_window else None,
        "trend": delta, "last_active": last_active, "days_inactive": inactive, "plan_overdue": overdue, "series": series[-SPARK_POINTS:],
    }


def batch_progress(batch, days=30, now=None) -> dict:
    now = now or timezone.now()
    students = list(batch.students.filter(is_active=True).order_by("first_name", "username"))
    ids = [s.id for s in students]
    since = now - timedelta(days=180)
    by_student = {i: [] for i in ids}
    done = Attempt.objects.filter(student_id__in=ids, status=Attempt.Status.SUBMITTED, exam__is_practice=False, score__isnull=False,
                                  submitted_at__gte=since)
    for a in done:
        by_student[a.student_id].append(a)
    last = {}
    for sid, started in Attempt.objects.filter(student_id__in=ids).values_list("student_id", "started_at"):
        if sid not in last or started > last[sid]:
            last[sid] = started
    today = timezone.localdate(now)
    overdue = {}
    missed = StudyTask.objects.filter(plan__student_id__in=ids, done=False, date__lt=today).exclude(kind=StudyTask.Kind.EXAM_DAY)
    for sid in missed.values_list("plan__student_id", flat=True):
        overdue[sid] = overdue.get(sid, 0) + 1
    rows = [student_row(s, by_student[s.id], last.get(s.id), overdue.get(s.id, 0), now, days) for s in students]
    rows.sort(key=lambda r: (STATUS_ORDER[r["status"]], -(r["days_inactive"] or 0), r["name"].lower()))
    counts = {k: sum(1 for r in rows if r["status"] == k) for k in STATUS_ORDER}
    averages = [r["average_percent"] for r in rows if r["average_percent"] is not None]
    return {
        "batch": {"id": batch.id, "name": batch.name}, "days": days, "students": len(rows), "counts": counts,
        "average_percent": round(_mean(averages), 1) if averages else None, "results": rows,
    }


def as_csv(report: dict) -> str:
    out = StringIO()
    w = csv.writer(out)
    w.writerow(["Name", "Username", "Status", "Why", "Tests taken", "Average %", "Trend (points)", "Days inactive", "Plan tasks missed"])
    for r in report["results"]:
        w.writerow([r["name"], r["username"], r["status"], "; ".join(r["reasons"]), r["tests_taken"],
                    "" if r["average_percent"] is None else r["average_percent"], "" if r["trend"] is None else r["trend"],
                    "" if r["days_inactive"] is None else r["days_inactive"], r["plan_overdue"]])
    return out.getvalue()
