"""Read-only statistics shared by the admin dashboard, student profiles and PDF reports."""
from datetime import timedelta

from django.db.models import Avg, Count, Max, Q, Sum
from django.db.models.functions import TruncDate
from django.utils import timezone

from accounts.models import Role, User
from attempts.models import Attempt, AttemptQuestion
from exams.models import Exam
from questionbank.models import Question

SUBMITTED = Attempt.Status.SUBMITTED


def pct(score, total):
    if score is None or not total or not float(total):
        return None
    return round(float(score) / float(total) * 100, 1)


def name_of(u: User) -> str:
    return (f"{u.first_name} {u.last_name}").strip() or u.username


def subject_breakdown(attempt_ids) -> list[dict]:
    rows = (
        AttemptQuestion.objects.filter(attempt__in=attempt_ids)
        .values("subject_name")
        .annotate(
            correct=Count("id", filter=Q(is_correct=True)),
            wrong=Count("id", filter=Q(is_correct=False)),
            unanswered=Count("id", filter=Q(is_correct__isnull=True)),
            marks_sum=Sum("marks_awarded"),
            marks_max=Sum("marks"),
        )
        .order_by("subject_name")
    )
    out = []
    for r in rows:
        answered = r["correct"] + r["wrong"]
        out.append({
            "subject": r["subject_name"] or "General",
            "correct": r["correct"], "incorrect": r["wrong"], "unanswered": r["unanswered"],
            "accuracy": round(r["correct"] * 100 / answered, 1) if answered else 0,
            "marks": str(r["marks_sum"] or 0), "max_marks": str(r["marks_max"] or 0),
        })
    return out


def student_performance(student: User) -> dict:
    """Everything an admin needs to know about one student's attempts (admins see unreleased results too)."""
    done = list(Attempt.objects.filter(student=student, status=SUBMITTED).select_related("exam").order_by("submitted_at"))
    pcts = [p for p in (pct(a.score, a.total_marks) for a in done) if p is not None]
    correct = sum(a.correct_count for a in done)
    answered = correct + sum(a.incorrect_count for a in done)
    history = []
    for a in Attempt.objects.filter(student=student).select_related("exam").order_by("-started_at"):
        history.append({
            "id": a.id, "exam": a.exam_id, "exam_title": a.exam.title, "attempt_number": a.attempt_number,
            "status": a.status, "started_at": a.started_at, "submitted_at": a.submitted_at,
            "score": str(a.score) if a.score is not None else None, "total_marks": str(a.total_marks),
            "percentage": pct(a.score, a.total_marks), "passed": a.passed,
            "auto_submitted": a.auto_submitted, "time_taken_seconds": a.time_taken_seconds,
        })
    return {
        "attempts_count": len(done),
        "in_progress_count": Attempt.objects.filter(student=student, status=Attempt.Status.IN_PROGRESS).count(),
        "average_percentage": round(sum(pcts) / len(pcts), 1) if pcts else None,
        "best_percentage": max(pcts) if pcts else None,
        "accuracy_percent": round(correct * 100 / answered, 1) if answered else None,
        "pass_rate": round(sum(1 for a in done if a.passed) * 100 / len(done), 1) if done else None,
        "subjects": subject_breakdown([a.id for a in done]),
        "trend": [
            {"attempt": a.id, "title": a.exam.title, "percentage": pct(a.score, a.total_marks), "submitted_at": a.submitted_at}
            for a in done[-10:]
        ],
        "history": history,
    }


def exam_performance(exam: Exam) -> dict:
    done = Attempt.objects.filter(exam=exam, status=SUBMITTED)
    agg = done.aggregate(n=Count("id"), avg=Avg("score"), hi=Max("score"))
    lo = done.order_by("score").values_list("score", flat=True).first()
    n = agg["n"]
    passed = done.filter(passed=True).count()
    best = {}
    for a in done.select_related("student").order_by("-score"):
        best.setdefault(a.student_id, a)
    counts = dict(done.values_list("student").annotate(c=Count("id")))
    students = [
        {
            "name": name_of(a.student), "email": a.student.email, "attempts": counts.get(a.student_id, 0),
            "best_score": str(a.score), "total_marks": str(a.total_marks), "percentage": pct(a.score, a.total_marks),
            "passed": a.passed,
        }
        for a in best.values()
    ]
    return {
        "attempts": n, "students": len(best),
        "average_score": None if agg["avg"] is None else round(float(agg["avg"]), 2),
        "highest_score": None if agg["hi"] is None else float(agg["hi"]),
        "lowest_score": None if lo is None else float(lo),
        "pass_rate": round(passed * 100 / n, 1) if n else None,
        "rows": students,
    }


def dashboard() -> dict:
    now = timezone.now()
    students = User.objects.filter(role=Role.STUDENT)
    done = Attempt.objects.filter(status=SUBMITTED)

    days = [(now - timedelta(days=i)).date() for i in range(13, -1, -1)]
    by_day = dict(
        done.filter(submitted_at__date__gte=days[0])
        .annotate(d=TruncDate("submitted_at"))
        .values_list("d").annotate(c=Count("id"))
    )

    pcts = [p for p in (pct(a.score, a.total_marks) for a in done.only("score", "total_marks")) if p is not None]
    buckets = [0] * 10
    for p in pcts:
        buckets[min(int(max(p, 0) // 10), 9)] += 1

    subjects = subject_breakdown(done.values_list("id", flat=True))
    top = (
        Exam.objects.annotate(n=Count("attempts", filter=Q(attempts__status=SUBMITTED)))
        .filter(n__gt=0).order_by("-n")[:5]
    )
    recent = done.select_related("student", "exam").order_by("-submitted_at")[:8]
    return {
        "students": {
            "total": students.count(),
            "active": students.filter(is_active=True).count(),
            "new_last_30_days": students.filter(date_joined__gte=now - timedelta(days=30)).count(),
        },
        "questions": {
            "total": Question.objects.count(),
            "active": Question.objects.filter(is_active=True).count(),
        },
        "tests": {
            "total": Exam.objects.count(),
            "published": Exam.objects.filter(status=Exam.Status.PUBLISHED).count(),
            "draft": Exam.objects.filter(status=Exam.Status.DRAFT).count(),
            "archived": Exam.objects.filter(status=Exam.Status.ARCHIVED).count(),
        },
        "attempts": {
            "submitted": done.count(),
            "in_progress": Attempt.objects.filter(status=Attempt.Status.IN_PROGRESS).count(),
            "participating_students": done.values("student").distinct().count(),
        },
        "average_percentage": round(sum(pcts) / len(pcts), 1) if pcts else None,
        "pass_rate": round(done.filter(passed=True).count() * 100 / done.count(), 1) if done.exists() else None,
        "participation": [{"date": str(d), "attempts": by_day.get(d, 0)} for d in days],
        "score_distribution": [{"range": f"{i * 10}-{i * 10 + 10}%", "count": c} for i, c in enumerate(buckets)],
        "subjects": subjects,
        "top_tests": [{"id": e.id, "title": e.title, "attempts": e.n} for e in top],
        "recent_activity": [
            {
                "attempt": a.id, "student": name_of(a.student), "exam": a.exam.title,
                "score": str(a.score), "total_marks": str(a.total_marks), "percentage": pct(a.score, a.total_marks),
                "passed": a.passed, "submitted_at": a.submitted_at,
            }
            for a in recent
        ],
    }
