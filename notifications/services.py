"""Creating notifications, and working out who should get them."""
from datetime import timedelta

from django.db.models import Q
from django.utils import timezone

from accounts.models import Role, User
from exams.models import Batch, Exam, ExamCategory, Preparation

from .models import Notification

CHUNK = 500


def notify(users, kind, title, body="", link="", ref="") -> int:
    """Sends one message to each user (an iterable of users or ids). A repeated `ref` for the same person is skipped."""
    ids = [u.id if hasattr(u, "id") else u for u in users]
    if not ids:
        return 0
    before = Notification.objects.filter(user_id__in=ids, ref=ref).count() if ref else 0
    Notification.objects.bulk_create(
        [Notification(user_id=i, kind=kind, title=title[:200], body=body, link=link[:200], ref=ref) for i in ids],
        batch_size=CHUNK, ignore_conflicts=True,
    )
    return len(ids) - before if ref else len(ids)


def students_for_exam(exam: Exam):
    """Who can take this test: its assigned students or batches, or for a public test those preparing for its exam."""
    active = User.objects.filter(role=Role.STUDENT, is_active=True)
    if exam.access_type == Exam.Access.ASSIGNED:
        return active.filter(assigned_exams=exam)
    if exam.access_type == Exam.Access.BATCH:
        return active.filter(student_batches__in=exam.assigned_batches.filter(is_active=True)).distinct()
    if exam.category_id:
        return active.filter(preparations__category_id=exam.category_id).distinct()
    return active.none()


def audience(kind: str, target_id=None):
    """Recipients of an announcement: all students, the students preparing for an exam, or a batch."""
    active = User.objects.filter(role=Role.STUDENT, is_active=True)
    if kind == "all":
        return active
    if kind == "exam":
        return active.filter(preparations__category_id=target_id).distinct()
    if kind == "batch":
        return active.filter(student_batches__id=target_id).distinct()
    raise ValueError(kind)


def test_link(exam: Exam) -> str:
    return f"/student/tests/{exam.public_id}"


def on_test_published(exam: Exam) -> int:
    if exam.is_practice:
        return 0
    return notify(students_for_exam(exam), Notification.Kind.NEW_TEST, f"New test: {exam.title}",
                  f"{exam.duration_minutes} minutes", test_link(exam), ref=f"new:{exam.id}")


def on_results_released(exam: Exam) -> int:
    from attempts.models import Attempt

    ids = Attempt.objects.filter(exam=exam, status=Attempt.Status.SUBMITTED).values_list("student_id", flat=True).distinct()
    return notify(list(ids), Notification.Kind.RESULT, f"Your result is ready: {exam.title}", "", f"/student/attempts", ref=f"result:{exam.id}")


def send_start_reminders(now=None, within=timedelta(hours=1)) -> int:
    """For every published test starting within `within`, remind the students who can take it (once each)."""
    now = now or timezone.now()
    sent = 0
    due = Exam.objects.filter(status=Exam.Status.PUBLISHED, is_practice=False, start_at__gt=now, start_at__lte=now + within)
    for exam in due:
        when = timezone.localtime(exam.start_at).strftime("%d %b, %I:%M %p")
        sent += notify(students_for_exam(exam), Notification.Kind.REMINDER, f"Starting soon: {exam.title}",
                       f"The test opens at {when}.", test_link(exam), ref=f"start:{exam.id}")
    return sent


def send_weekly_summaries(today=None) -> dict:
    """
    A note of each active student's week, in the bell and, when emails are set up, by email. Students who turned it off, and
    students who did nothing this week, get nothing. Safe to run twice in a week: the bell message is sent once per week.
    """
    from django.core.mail import send_mail
    from django.conf import settings

    from accounts.emails import email_enabled
    from attempts import weekly

    today = today or timezone.localdate()
    year, week, _ = today.isocalendar()
    sent = emailed = 0
    for student in User.objects.filter(role=Role.STUDENT, is_active=True, weekly_summary=True).iterator():
        data = weekly.summary(student, today=today)
        if not weekly.has_activity(data):
            continue
        title, body = weekly.message(data)
        if not notify([student], Notification.Kind.WEEKLY, title, body, "/student/dashboard", ref=f"weekly:{year}-{week}"):
            continue
        sent += 1
        if email_enabled() and student.email:
            try:
                send_mail(title, f"Hello {student.first_name or student.username},\n\n{body}\n\nKeep going!\n"
                          f"{settings.FRONTEND_URL.rstrip('/')}/student/dashboard\n", settings.DEFAULT_FROM_EMAIL, [student.email])
                emailed += 1
            except Exception:  # noqa: BLE001 - one bad address must not stop the rest
                pass
    return {"notified": sent, "emailed": emailed}
