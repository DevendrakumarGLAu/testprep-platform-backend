import random
from datetime import timedelta
from decimal import Decimal

from django.db import IntegrityError, transaction
from django.db.models import Q
from django.utils import timezone

from exams.models import Exam, ExamQuestion

from .models import Attempt, AttemptQuestion, AttemptSection

# Allowance for network latency when a student saves right at the deadline.
GRACE_SECONDS = 5


# --- visibility and eligibility -------------------------------------------------------------

def visible_exams(user):
    """Published tests this student is allowed to see."""
    return (
        Exam.objects.filter(status=Exam.Status.PUBLISHED)
        .filter(
            Q(access_type=Exam.Access.PUBLIC)
            | Q(access_type=Exam.Access.ASSIGNED, assigned_students=user)
            | Q(access_type=Exam.Access.BATCH, assigned_batches__students=user, assigned_batches__is_active=True)
        )
        .distinct()
    )


def completed_count(exam, user) -> int:
    return Attempt.objects.filter(exam=exam, student=user, status=Attempt.Status.SUBMITTED).count()


def student_state(exam, user, now=None, active=None, used=None):
    """Returns (status, can_start, reason) for one student and one visible test."""
    now = now or timezone.now()
    if active is None:
        active = Attempt.objects.filter(exam=exam, student=user, status=Attempt.Status.IN_PROGRESS).first()
    if used is None:
        used = completed_count(exam, user)

    if active:
        return "in_progress", True, "You have an attempt in progress."
    if exam.start_at and now < exam.start_at:
        return "upcoming", False, "This test has not started yet."
    if exam.end_at and now > exam.end_at:
        return ("completed" if used else "missed"), False, "The availability window has closed."
    if exam.max_attempts and used >= exam.max_attempts:
        return "completed", False, "You have used all your attempts."
    return "available", True, ""


def result_visible(exam, now=None) -> bool:
    now = now or timezone.now()
    if exam.result_release == Exam.ResultRelease.IMMEDIATE:
        return True
    if exam.result_release == Exam.ResultRelease.SCHEDULED:
        return bool(exam.result_release_at and now >= exam.result_release_at)
    return exam.results_released


def solutions_visible(exam, now=None) -> bool:
    now = now or timezone.now()
    if not result_visible(exam, now):
        return False
    if exam.solution_release == Exam.SolutionRelease.WITH_RESULT:
        return True
    if exam.solution_release == Exam.SolutionRelease.AFTER_END:
        return bool(exam.end_at and now > exam.end_at)
    return exam.solutions_released


# --- starting -------------------------------------------------------------------------------

class CannotStart(Exception):
    def __init__(self, reason):
        self.reason = reason


def start_attempt(exam: Exam, user) -> tuple[Attempt, bool]:
    """Create the attempt or resume the active one. Safe to call repeatedly. Returns (attempt, created)."""
    try:
        with transaction.atomic():
            if not visible_exams(user).filter(pk=exam.pk).exists():
                raise CannotStart("You are not eligible for this test.")

            now = timezone.now()
            expire_overdue(Attempt.objects.filter(exam=exam, student=user))
            active = Attempt.objects.filter(exam=exam, student=user, status=Attempt.Status.IN_PROGRESS).first()
            if active:
                return active, False

            status, can_start, reason = student_state(exam, user, now)
            if not can_start:
                raise CannotStart(reason)

            items = list(
                ExamQuestion.objects.filter(section__exam=exam)
                .select_related("section", "question__subject")
                .prefetch_related("question__options")
            )
            if not items:
                raise CannotStart("This test has no questions.")

            deadline = now + timedelta(minutes=exam.duration_minutes)
            if exam.end_at and exam.end_at < deadline:
                deadline = exam.end_at  # the window closing ends the attempt, even mid-test

            number = Attempt.objects.filter(exam=exam, student=user).count() + 1
            attempt = Attempt.objects.create(
                exam=exam, student=user, attempt_number=number, exam_version=exam.version,
                deadline=deadline, duration_minutes=exam.duration_minutes,
                total_marks=exam.total_marks, passing_marks=exam.passing_marks,
                negative_marking_enabled=exam.negative_marking_enabled,
                unanswered_penalty=exam.unanswered_penalty, sectional_timing=exam.sectional_timing,
            )

            sections: dict[int, list] = {}
            for it in items:
                sections.setdefault(it.section_id, []).append(it)
            ordered_sections = sorted(sections.values(), key=lambda lst: (lst[0].section.order, lst[0].section_id))

            order = 0
            rows = []
            for lst in ordered_sections:
                lst.sort(key=lambda i: (i.order, i.id))
                if exam.shuffle_questions:
                    random.shuffle(lst)
                for it in lst:
                    q = it.question
                    opts = [{"id": o.id, "text": o.text, "text_hi": o.text_hi, "is_correct": o.is_correct} for o in q.options.all()]
                    if exam.shuffle_options:
                        random.shuffle(opts)
                    rows.append(AttemptQuestion(
                        attempt=attempt, question=q, section_name=it.section.name,
                        section_order=it.section.order, order=order, subject_name=q.subject.name,
                        text=q.text, text_hi=q.text_hi, image_path=q.image.name if q.image else "", options=opts,
                        explanation=q.explanation, explanation_hi=q.explanation_hi, marks=it.marks, negative_marks=it.negative_marks,
                    ))
                    order += 1
            AttemptQuestion.objects.bulk_create(rows)
            if exam.sectional_timing:
                AttemptSection.objects.bulk_create(
                    AttemptSection(
                        attempt=attempt, name=lst[0].section.name, order=lst[0].section.order,
                        duration_seconds=(lst[0].section.duration_minutes or 0) * 60,
                        started_at=attempt.started_at if i == 0 else None,
                    )
                    for i, lst in enumerate(ordered_sections)
                )
            return attempt, True
    except IntegrityError:
        # A parallel request created the attempt first (unique "one active attempt" constraint).
        active = Attempt.objects.filter(exam=exam, student=user, status=Attempt.Status.IN_PROGRESS).first()
        if active:
            return active, False
        raise


# --- submitting and scoring -----------------------------------------------------------------

def _q(value) -> Decimal:
    return Decimal(value).quantize(Decimal("0.01"))


def score_attempt(attempt: Attempt, submitted_at) -> None:
    """Computes and stores the result. Only ever called from submit_attempt, inside a transaction."""
    correct = incorrect = unanswered = 0
    total = Decimal("0")
    rows = list(attempt.questions.all())
    for aq in rows:
        if aq.selected_option_id is None:
            unanswered += 1
            aq.is_correct = None
            aq.marks_awarded = -attempt.unanswered_penalty
        else:
            chosen = next((o for o in aq.options if o["id"] == aq.selected_option_id), None)
            if chosen and chosen["is_correct"]:
                correct += 1
                aq.is_correct = True
                aq.marks_awarded = aq.marks
            else:
                incorrect += 1
                aq.is_correct = False
                aq.marks_awarded = -(aq.negative_marks if attempt.negative_marking_enabled else Decimal("0"))
        total += aq.marks_awarded
    AttemptQuestion.objects.bulk_update(rows, ["is_correct", "marks_awarded"])

    answered = correct + incorrect
    attempt.score = _q(total)
    attempt.correct_count, attempt.incorrect_count, attempt.unanswered_count = correct, incorrect, unanswered
    attempt.accuracy = _q(Decimal(correct) * 100 / answered) if answered else Decimal("0.00")
    attempt.passed = attempt.score >= attempt.passing_marks
    elapsed = (submitted_at - attempt.started_at).total_seconds()
    attempt.time_taken_seconds = int(max(0, min(elapsed, attempt.duration_minutes * 60)))


def submit_attempt(attempt_id: int, auto: bool = False) -> Attempt:
    """Idempotent: a second call returns the already-scored attempt untouched."""
    with transaction.atomic():
        attempt = Attempt.objects.select_for_update().get(pk=attempt_id)
        if attempt.status == Attempt.Status.SUBMITTED:
            return attempt
        now = timezone.now()
        attempt.status = Attempt.Status.SUBMITTED
        attempt.submitted_at = min(now, attempt.deadline) if auto else now
        attempt.auto_submitted = auto
        score_attempt(attempt, attempt.submitted_at)
        attempt.save()
        return attempt


def expire_overdue(qs=None) -> int:
    """Auto-submits every in-progress attempt whose deadline has passed."""
    qs = qs if qs is not None else Attempt.objects.all()
    ids = list(qs.filter(status=Attempt.Status.IN_PROGRESS, deadline__lt=timezone.now()).values_list("id", flat=True))
    for i in ids:
        submit_attempt(i, auto=True)
    return len(ids)


def remaining_seconds(attempt: Attempt, now=None) -> int:
    now = now or timezone.now()
    return max(0, int((attempt.deadline - now).total_seconds()))


def summary(attempt: Attempt) -> dict:
    return {
        "score": str(attempt.score), "total_marks": str(attempt.total_marks),
        "passing_marks": str(attempt.passing_marks), "passed": attempt.passed,
        "correct": attempt.correct_count, "incorrect": attempt.incorrect_count,
        "unanswered": attempt.unanswered_count, "accuracy": str(attempt.accuracy),
        "time_taken_seconds": attempt.time_taken_seconds,
    }



# --- sectional timing ------------------------------------------------------------------------

def advance_sections(attempt: Attempt, now=None) -> list[AttemptSection]:
    """
    Closes, in order, every section whose time has run out, and starts the next one at that moment.
    Returns all section states. Does nothing for ordinary tests. Safe to call as often as needed.
    """
    states = list(attempt.section_states.order_by("order"))
    if not attempt.sectional_timing or attempt.status != Attempt.Status.IN_PROGRESS:
        return states
    now = now or timezone.now()
    previous_end = attempt.started_at
    dirty: list[AttemptSection] = []
    for st in states:
        if st.ended_at is not None:
            previous_end = st.ended_at
            continue
        if st.started_at is None:
            st.started_at = previous_end
            dirty.append(st)
        end = st.started_at + timedelta(seconds=st.duration_seconds)
        if now >= end:
            st.ended_at, st.closed_by = end, AttemptSection.ClosedBy.EXPIRED
            if st not in dirty:
                dirty.append(st)
            previous_end = end
            continue
        break
    if dirty:
        AttemptSection.objects.bulk_update(dirty, ["started_at", "ended_at", "closed_by"])
    return states


def active_section(states: list[AttemptSection]):
    return next((s for s in states if s.ended_at is None), None)


def section_remaining_seconds(st: AttemptSection, now=None) -> int:
    now = now or timezone.now()
    return max(0, int((st.started_at + timedelta(seconds=st.duration_seconds) - now).total_seconds()))


def open_section_orders(states: list[AttemptSection], now) -> set[int]:
    """Sections that may still receive answers: the active one, plus one that just timed out (network slack)."""
    orders = set()
    for st in states:
        if st.ended_at is None:
            orders.add(st.order)
            break
        if st.closed_by == AttemptSection.ClosedBy.EXPIRED and now <= st.ended_at + timedelta(seconds=GRACE_SECONDS):
            orders.add(st.order)
    return orders


def _projected_deadline(attempt: Attempt, states: list[AttemptSection]):
    """When the attempt ends if every remaining section is allowed to run its full time."""
    pending = [s for s in states if s.ended_at is None]
    if not pending:
        return attempt.deadline
    start = pending[0].started_at or attempt.started_at
    end = start + timedelta(seconds=sum(s.duration_seconds for s in pending))
    cap = attempt.exam.end_at
    return min(end, cap) if cap else end


def submit_section(attempt_id: int, order: int) -> tuple[Attempt, bool]:
    """
    The student submits the section they are in. Returns (attempt, moved). Idempotent: if that section is no longer
    the active one (double click, or the timer got there first) nothing changes. Submitting the last section
    submits the whole test.
    """
    with transaction.atomic():
        attempt = Attempt.objects.select_for_update().select_related("exam").get(pk=attempt_id)
        if attempt.status != Attempt.Status.IN_PROGRESS:
            return attempt, False
        states = advance_sections(attempt)
        active = active_section(states)
        if active is None:  # every section has run out
            return submit_attempt(attempt.id, auto=True), False
        if order != active.order:
            return attempt, False
        if active is states[-1]:
            return submit_attempt(attempt.id), True
        now = timezone.now()
        nxt = states[states.index(active) + 1]
        active.ended_at, active.closed_by = now, AttemptSection.ClosedBy.SUBMITTED
        nxt.started_at = now
        AttemptSection.objects.bulk_update([active, nxt], ["started_at", "ended_at", "closed_by"])
        attempt.deadline = _projected_deadline(attempt, states)
        attempt.save(update_fields=["deadline"])
        return attempt, True
