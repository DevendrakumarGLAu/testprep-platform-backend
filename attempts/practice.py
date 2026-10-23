"""
Practice sets: short tests a student builds for themselves from their own history.

Each one is an ordinary Exam flagged `is_practice`, assigned only to its owner and hidden from every list, so it runs through
the normal exam screen, timer, scoring and review without any special code in the engine.
"""
import datetime
import math
import random
from decimal import Decimal

from django.db.models import Count, Q
from django.utils import timezone

from exams.models import Exam, ExamQuestion, ExamSection
from questionbank.models import Question

from .models import Attempt, AttemptQuestion

MAX_PRACTICE_QUESTIONS = 50
MIN_ANSWERED_FOR_WEAK = 3  # a topic needs this many answers before it is judged
WEAK_BELOW_PERCENT = 60
DAILY_QUIZ_SIZE = 10
DAILY_QUIZ_WINDOW_DAYS = 45
DAILY_QUIZ_MIN = 5


class PracticeError(Exception):
    """Something the student can act on, shown as a message."""

    def __init__(self, message, status=400):
        super().__init__(message)
        self.message, self.status = message, status


def build_practice_exam(student, questions, title, kind, max_attempts=0) -> Exam:
    """Creates the hidden test for `student` from these questions (one section, 1 mark each, 1/4 off, 1 minute per question)."""
    questions = list(questions)[:MAX_PRACTICE_QUESTIONS]
    if not questions:
        raise PracticeError("There are no questions to practise yet.", 404)
    now = timezone.now()
    exam = Exam.objects.create(
        title=title[:200], description="A practice set made for you.", is_practice=True, practice_kind=kind, owner=student,
        status=Exam.Status.PUBLISHED, published_at=now, access_type=Exam.Access.ASSIGNED, created_by=student,
        duration_minutes=max(5, math.ceil(len(questions) * 1.0)), marks_per_question=Decimal("1"),
        negative_marking_enabled=True, negative_mode=Exam.NegativeMode.FRACTION, negative_value=Decimal("0.25"),
        max_attempts=max_attempts, result_release=Exam.ResultRelease.IMMEDIATE, solution_release=Exam.SolutionRelease.WITH_RESULT,
    )
    exam.assigned_students.add(student)
    section = ExamSection.objects.create(exam=exam, name="Practice", order=0)
    ExamQuestion.objects.bulk_create(
        ExamQuestion(
            section=section, question=q, order=i, marks=Decimal("1"), negative_marks=Decimal("0.25"), question_version=q.version
        )
        for i, q in enumerate(questions)
    )
    return exam


# --- weak topics ----------------------------------------------------------------------------

def topic_stats(student):
    """Per topic (or per subject when a question has no topic): how many answers and how many were right."""
    rows = (
        AttemptQuestion.objects.filter(
            attempt__student=student, attempt__status=Attempt.Status.SUBMITTED, question__isnull=False, is_correct__isnull=False
        )
        .values("question__subject", "question__subject__name", "question__topic", "question__topic__name")
        .annotate(answered=Count("id"), correct=Count("id", filter=Q(is_correct=True)))
    )
    out = []
    for r in rows:
        accuracy = round(r["correct"] * 100 / r["answered"], 1) if r["answered"] else 0
        out.append({
            "subject_id": r["question__subject"], "subject": r["question__subject__name"],
            "topic_id": r["question__topic"], "topic": r["question__topic__name"] or "General",
            "answered": r["answered"], "correct": r["correct"], "accuracy": accuracy,
        })
    return out


def weak_topics(student, limit=8):
    """Topics answered at least MIN_ANSWERED_FOR_WEAK times with accuracy below WEAK_BELOW_PERCENT, weakest first."""
    weak = [t for t in topic_stats(student) if t["answered"] >= MIN_ANSWERED_FOR_WEAK and t["accuracy"] < WEAK_BELOW_PERCENT]
    return sorted(weak, key=lambda t: (t["accuracy"], -t["answered"]))[:limit]


def _already_wrong(student):
    return set(
        AttemptQuestion.objects.filter(attempt__student=student, attempt__status=Attempt.Status.SUBMITTED, is_correct=False)
        .exclude(question=None).values_list("question_id", flat=True)
    )


def weak_topic_questions(student, topics, count):
    """
    Active questions from the chosen topics, those the student got wrong before first, then ones they have not seen,
    then the rest. `topics` is a list of {"topic_id", "subject_id"} pairs; a null topic means the whole subject.
    """
    match = Q()
    for t in topics:
        match |= Q(topic_id=t["topic_id"]) if t.get("topic_id") else Q(subject_id=t["subject_id"], topic__isnull=True)
    pool = list(Question.objects.filter(match, is_active=True))
    wrong = _already_wrong(student)
    seen = set(AttemptQuestion.objects.filter(attempt__student=student).exclude(question=None).values_list("question_id", flat=True))
    random.shuffle(pool)
    pool.sort(key=lambda q: 0 if q.id in wrong else (1 if q.id not in seen else 2))
    return pool[:count]


def wrong_answer_questions(attempt, include_unanswered=True):
    flt = Q(is_correct=False) | (Q(is_correct__isnull=True) if include_unanswered else Q())
    qs = attempt.questions.filter(flt).exclude(question=None).select_related("question").order_by("order")
    return [aq.question for aq in qs if aq.question.is_active]


# --- daily quiz -----------------------------------------------------------------------------

def daily_quiz_title(day) -> str:
    return f"Daily quiz {day.isoformat()}"


def todays_quiz(student, today=None):
    today = today or timezone.localdate()
    return Exam.objects.filter(is_practice=True, practice_kind="daily", owner=student, title=daily_quiz_title(today)).first()


def daily_quiz_questions(student, today=None):
    """Random recent questions (by question date), preferring ones the student has not answered yet."""
    today = today or timezone.localdate()
    since = today - datetime.timedelta(days=DAILY_QUIZ_WINDOW_DAYS)
    pool = list(Question.objects.filter(is_active=True, question_date__gte=since, question_date__lte=today))
    seen = set(AttemptQuestion.objects.filter(attempt__student=student).exclude(question=None).values_list("question_id", flat=True))
    random.shuffle(pool)
    pool.sort(key=lambda q: q.id in seen)  # unseen first (sort is stable, so the shuffle still decides within each group)
    return pool[:DAILY_QUIZ_SIZE]
