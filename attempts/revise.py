"""Revision cards: the questions a student saved, or the ones they still get wrong, to go through one by one."""
import random

from .models import Attempt, AttemptQuestion, SavedQuestion
from . import services as svc

DEFAULT_LIMIT = 20
MAX_LIMIT = 50


def _options(options):
    return [{"id": o.get("id"), "text": o.get("text", ""), "text_hi": o.get("text_hi", ""), "is_correct": bool(o.get("is_correct"))} for o in options or []]


NO_EXAM = "none"  # the id used for cards of a test that belongs to no exam


def _exams_of(exam):
    """The exams (categories) a test shows in: its own and the ones it is shared into."""
    if exam is None:
        return []
    cats = ([exam.category] if exam.category_id else []) + [c for c in exam.also_in.all() if c.id != exam.category_id]
    return cats


def _card(key, subject, text, text_hi, options, selected, explanation, explanation_hi, exam_title="", exam=None):
    return {
        "key": key, "subject": subject, "exam_title": exam_title, "text": text, "text_hi": text_hi, "options": _options(options),
        "your_option": selected, "explanation": explanation, "explanation_hi": explanation_hi,
        "_exams": _exams_of(exam),  # worked out for filtering, and removed before the card is sent
    }


def saved_cards(student):
    rows = SavedQuestion.objects.filter(student=student).select_related("attempt_question__attempt__exam__category__group")
    rows = rows.prefetch_related("attempt_question__attempt__exam__also_in__group")
    out = []
    for s in rows:
        exam = s.attempt_question.attempt.exam if s.attempt_question_id else None
        out.append(_card(f"s{s.id}", s.subject_name, s.text, s.text_hi, s.options, s.selected_option_id, s.explanation, s.explanation_hi, s.exam_title, exam))
    return out


def mistake_cards(student):
    """
    One card per question the student's latest answer to is wrong (or skipped). A question they answered correctly later is left out.
    Only tests whose solutions are open to them are used, so a card never shows an answer early.
    """
    attempts = {
        a.id: a for a in Attempt.objects.filter(student=student, status=Attempt.Status.SUBMITTED).select_related("exam__category__group").prefetch_related("exam__also_in__group").order_by("-submitted_at", "-id")
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
        _card(f"m{aq.id}", aq.subject_name, aq.text, aq.text_hi, aq.options, aq.selected_option_id, aq.explanation, aq.explanation_hi, attempts[aq.attempt_id].exam.title, attempts[aq.attempt_id].exam)
        for aq in latest.values() if aq.is_correct is not True
    ]


def cards(student, source, limit, exam=None):
    """
    (total, cards, exams): `exams` lists every exam the student has cards for, with how many, so the screen can offer a filter; `exam`
    (an exam's public id, or "none") keeps only the cards of that exam. A test shared into several exams counts for each of them.
    """
    pool = saved_cards(student) if source == "saved" else mistake_cards(student)
    counts: dict = {}
    for c in pool:
        for e in c["_exams"] or [None]:
            key = e.public_id if e else NO_EXAM
            row = counts.setdefault(key, {"id": key, "name": e.name if e else "Other tests", "group": (e.group.name if e and e.group else None), "count": 0})
            row["count"] += 1
    exams = sorted(counts.values(), key=lambda r: (r["id"] == NO_EXAM, r["group"] or "~", r["name"].lower()))
    if exam:
        pool = [c for c in pool if any(e.public_id == exam for e in c["_exams"]) or (exam == NO_EXAM and not c["_exams"])]
    random.shuffle(pool)
    for c in pool:
        c.pop("_exams")
    return len(pool), pool[:limit], exams
