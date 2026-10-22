"""Builds the detailed result of a submitted attempt, for the student and for admins."""
from collections import OrderedDict
from decimal import Decimal

from django.conf import settings

from core.numbers import money

from .models import Attempt


def _pct(score, total) -> float | None:
    return round(float(score) / float(total) * 100, 1) if total and float(total) else None


def _image(request, path):
    if not path:
        return None
    return request.build_absolute_uri("/" + settings.MEDIA_URL.strip("/") + "/" + path)


def exam_average_percentage(exam) -> dict:
    rows = Attempt.objects.filter(exam=exam, status=Attempt.Status.SUBMITTED, score__isnull=False)
    pcts = [p for p in (_pct(a.score, a.total_marks) for a in rows) if p is not None]
    return {
        "attempts_count": len(pcts),
        "average_percentage": round(sum(pcts) / len(pcts), 1) if pcts else None,
    }


def build_result(attempt: Attempt, request, reveal_solutions: bool, include_student: bool = False) -> dict:
    """
    reveal_solutions=False still shows totals, section/subject marks and what the student chose,
    but not which options were right, per-question marks, or explanations.
    """
    rows = list(attempt.questions.all())

    sections: "OrderedDict[tuple, dict]" = OrderedDict()
    subjects: dict[str, dict] = {}
    for aq in rows:
        s = sections.setdefault(
            (aq.section_order, aq.section_name),
            {"name": aq.section_name, "marks": Decimal("0"), "max_marks": Decimal("0"), "correct": 0, "incorrect": 0, "unanswered": 0, "time_seconds": 0},
        )
        sub = subjects.setdefault(
            aq.subject_name or "General",
            {"subject": aq.subject_name or "General", "marks": Decimal("0"), "max_marks": Decimal("0"), "correct": 0, "answered": 0},
        )
        s["max_marks"] += aq.marks
        s["time_seconds"] += aq.time_spent_seconds
        sub["max_marks"] += aq.marks
        awarded = aq.marks_awarded or Decimal("0")
        s["marks"] += awarded
        sub["marks"] += awarded
        if aq.is_correct is None:
            s["unanswered"] += 1
        else:
            sub["answered"] += 1
            if aq.is_correct:
                s["correct"] += 1
                sub["correct"] += 1
            else:
                s["incorrect"] += 1

    questions = []
    for aq in rows:
        correct_id = next((o["id"] for o in aq.options if o["is_correct"]), None)
        item = {
            "number": aq.order + 1,
            "section": aq.section_name,
            "subject": aq.subject_name,
            "text": aq.text,
            "text_hi": aq.text_hi,
            "image_url": _image(request, aq.image_path),
            "marks": str(aq.marks),
            "options": [
                {"id": o["id"], "text": o["text"], "text_hi": o.get("text_hi", ""), **({"is_correct": o["is_correct"]} if reveal_solutions else {})}
                for o in aq.options
            ],
            "selected_option": aq.selected_option_id,
            "time_spent_seconds": aq.time_spent_seconds,
        }
        if reveal_solutions:
            item.update({
                "correct_option": correct_id,
                "status": "unanswered" if aq.is_correct is None else ("correct" if aq.is_correct else "incorrect"),
                "marks_awarded": money(aq.marks_awarded),
                "explanation": aq.explanation,
                "explanation_hi": aq.explanation_hi,
            })
        questions.append(item)

    total_time = sum(aq.time_spent_seconds for aq in rows)
    slowest = max(rows, key=lambda aq: aq.time_spent_seconds, default=None)
    timing = {
        "total_seconds": total_time,
        "average_seconds": round(total_time / len(rows)) if rows else 0,
        "slowest": {"number": slowest.order + 1, "seconds": slowest.time_spent_seconds} if slowest and slowest.time_spent_seconds else None,
    }
    summary = {
        "score": str(attempt.score), "total_marks": str(attempt.total_marks),
        "passing_marks": str(attempt.passing_marks), "passed": attempt.passed,
        "percentage": _pct(attempt.score, attempt.total_marks),
        "correct": attempt.correct_count, "incorrect": attempt.incorrect_count,
        "unanswered": attempt.unanswered_count, "accuracy": str(attempt.accuracy),
        "time_taken_seconds": attempt.time_taken_seconds,
    }
    data = {
        "attempt": {
            "id": attempt.public_id, "attempt_number": attempt.attempt_number, "submitted_at": attempt.submitted_at,
            "auto_submitted": attempt.auto_submitted,
        },
        "exam": {"id": attempt.exam.public_id, "title": attempt.exam.title},
        "summary": summary,
        "sections": [
            {**s, "marks": money(s["marks"]), "max_marks": money(s["max_marks"])} for s in sections.values()
        ],
        "subjects": [
            {
                "subject": v["subject"], "marks": money(v["marks"]), "max_marks": money(v["max_marks"]),
                "accuracy": round(v["correct"] * 100 / v["answered"], 1) if v["answered"] else 0,
            }
            for v in subjects.values()
        ],
        "timing": timing,
        "comparison": exam_average_percentage(attempt.exam),
        "solutions_available": reveal_solutions,
        "questions": questions,
    }
    if include_student:
        st = attempt.student
        data["attempt"]["student"] = {
            "id": st.id, "name": (f"{st.first_name} {st.last_name}").strip() or st.username, "email": st.email,
        }
    return data
