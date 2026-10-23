"""Revision cards: the questions a student saved, or the ones they still get wrong, to go through one by one."""
import random

from .models import Attempt, AttemptQuestion, SavedQuestion
from . import services as svc

DEFAULT_LIMIT = 20
MAX_LIMIT = 50


def _options(options):
    return [{"id": o.get("id"), "text": o.get("text", ""), "text_hi": o.get("text_hi", ""), "is_correct": bool(o.get("is_correct"))} for o in options or []]


def _card(key, subject, text, text_hi, options, selected, explanation, explanation_hi, exam_title=""):
    return {
        "key": key, "subject": subject, "exam_title": exam_title, "text": text, "text_hi": text_hi, "options": _options(options),
        "your_option": selected, "explanation": explanation, "explanation_hi": explanation_hi,
    }


def saved_cards(student):
    return [
        _card(f"s{s.id}", s.subject_name, s.text, s.text_hi, s.options, s.selected_option_id, s.explanation, s.explanation_hi, s.exam_title)
        for s in SavedQuestion.objects.filter(student=student)
    ]


def mistake_cards(student):
    """
    One card per question the student's latest answer to is wrong (or skipped). A question they answered correctly later is left out.
    Only tests whose solutions are open to them are used, so a card never shows an answer early.
    """
    attempts = {
        a.id: a for a in Attempt.objects.filter(student=student, status=Attempt.Status.SUBMITTED).select_related("exam").order_by("-submitted_at", "-id")
    }
    visible = {}
    allowed = [a for a in attempts.values() if visible.setdefault(a.exam_id, svc.solutions_visible(a.exam))]
    order = {a.id: i for i, a in enumerate(allowed)}  # newest attempt first
    rows = AttemptQuestion.objects.filter(attempt__in=list(order)).order_by("order")
    latest = {}
    for aq in sorted(rows, key=lambda r: (order[r.attempt_id], r.order)):
        key = aq.question_id or f"aq{aq.id}"
        latest.setdefault(key, aq)
    return [
        _card(f"m{aq.id}", aq.subject_name, aq.text, aq.text_hi, aq.options, aq.selected_option_id, aq.explanation, aq.explanation_hi, attempts[aq.attempt_id].exam.title)
        for aq in latest.values() if aq.is_correct is not True
    ]


def cards(student, source, limit):
    pool = saved_cards(student) if source == "saved" else mistake_cards(student)
    random.shuffle(pool)
    return len(pool), pool[:limit]
