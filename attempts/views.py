from collections import defaultdict
from datetime import timedelta
from decimal import Decimal

from django.conf import settings
from django.db import transaction
from django.db.models import Count, Q
from django.shortcuts import get_object_or_404
from django.utils import timezone
from rest_framework import serializers
from rest_framework.response import Response
from rest_framework.views import APIView

from accounts.permissions import IsStudent
from core.numbers import money
from exams.models import Exam, ExamCategory, Preparation
from questionbank.models import Question

from . import services as svc
from .models import Attempt, AttemptQuestion, AttemptSection
from . import activity
from .results import build_result


def _img(request, path):
    if not path:
        return None
    return request.build_absolute_uri("/" + settings.MEDIA_URL.strip("/") + "/" + path)


def exam_card(request, exam, user, now, active_by_exam, used_by_exam):
    active = active_by_exam.get(exam.id)
    used = used_by_exam.get(exam.id, 0)
    status, can_start, reason = svc.student_state(exam, user, now, active=active or False, used=used)
    locked = False
    if exam.is_premium:
        if not hasattr(request, "_premium_ids"):
            request._premium_ids = svc.premium_entitlements(user)  # worked out once per request, not once per card
        locked = exam.id not in request._premium_ids and not active
        if locked and can_start:
            status, can_start, reason = "locked", False, svc.PREMIUM_MESSAGE
    return {
        "id": exam.public_id,
        "title": exam.title,
        "description": exam.description,
        "category": exam.category.public_id if exam.category else None,
        "category_name": exam.category.name if exam.category else None,
        "test_type": exam.test_type,
        "test_type_label": exam.get_test_type_display(),
        "subject": exam.subject_id,
        "subject_name": exam.subject.name if exam.subject else None,
        "year": exam.year,
        "thumbnail_url": request.build_absolute_uri(exam.thumbnail.url) if exam.thumbnail else None,
        "question_count": exam.question_count,
        "total_marks": str(exam.total_marks),
        "duration_minutes": exam.duration_minutes,
        "start_at": exam.start_at,
        "end_at": exam.end_at,
        "max_attempts": exam.max_attempts,
        "attempts_used": used,
        "negative_marking_enabled": exam.negative_marking_enabled,
        "sectional_timing": exam.sectional_timing,
        "status": status,
        "can_start": can_start,
        "reason": reason,
        "active_attempt_id": active.public_id if active else None,
        "premium": exam.is_premium,
        "price": str(exam.price) if exam.price is not None else None,
        "locked": locked,
    }


def _context(user):
    """Active attempts and completed-attempt counts for the student, keyed by exam id."""
    svc.expire_overdue(Attempt.objects.filter(student=user))
    active = {a.exam_id: a for a in Attempt.objects.filter(student=user, status=Attempt.Status.IN_PROGRESS)}
    used = defaultdict(int)
    for exam_id in Attempt.objects.filter(student=user, status=Attempt.Status.SUBMITTED).values_list("exam_id", flat=True):
        used[exam_id] += 1
    return active, used


class StudentExamList(APIView):
    permission_classes = [IsStudent]

    def get(self, request):
        user, now = request.user, timezone.now()
        qs = svc.visible_exams(user).select_related("category", "subject").with_totals()
        if c := request.query_params.get("category"):
            qs = qs.filter(category__public_id=c)
        if s := request.query_params.get("subject"):
            qs = qs.filter(sections__items__question__subject_id=s).distinct()
        if t := request.query_params.get("test_type"):
            qs = qs.filter(test_type=t)
        if q := request.query_params.get("search"):
            qs = qs.filter(Q(title__icontains=q) | Q(description__icontains=q))
        active, used = _context(user)
        cards = [exam_card(request, e, user, now, active, used) for e in qs.order_by("start_at", "-created_at")]
        wanted = request.query_params.get("status")
        if wanted:
            cards = [c for c in cards if c["status"] == wanted]
        return Response(cards)


class StudentFilters(APIView):
    """Filter choices drawn only from tests this student can see, so nobody learns about hidden subjects."""

    permission_classes = [IsStudent]

    def get(self, request):
        from exams.models import ExamCategory
        from questionbank.models import Subject

        visible = svc.visible_exams(request.user)
        return Response({
            "categories": [
                {"id": c["public_id"], "name": c["name"]}
                for c in ExamCategory.objects.filter(exams__in=visible).distinct().order_by("name").values("public_id", "name")
            ],
            "subjects": list(Subject.objects.filter(questions__exam_items__section__exam__in=visible).distinct().order_by("name").values("id", "name")),
        })


def _prep_payload(user) -> dict:
    visible = svc.visible_exams(user).order_by()
    counts: dict[int, dict[str, int]] = defaultdict(lambda: defaultdict(int))
    rows = visible.exclude(category=None).values("category", "test_type").annotate(n=Count("id", distinct=True))
    for r in rows:
        counts[r["category"]][r["test_type"]] += r["n"]
    mine = set(Preparation.objects.filter(student=user).values_list("category_id", flat=True))
    public = dict(ExamCategory.objects.values_list("id", "public_id"))
    exams = []
    for c in ExamCategory.objects.filter(is_active=True).prefetch_related("subjects").order_by("name"):
        if c.id not in counts and c.id not in mine:
            continue  # nothing to prepare with yet
        k = counts[c.id]
        exams.append({
            "id": c.public_id, "name": c.name, "description": c.description,
            "subjects": [s.name for s in c.subjects.all()],
            "counts": {"full_test": k["full_test"], "sectional": k["sectional"], "previous_year": k["previous_year"],
                       "total": sum(k.values())},
            "preparing": c.id in mine,
        })
    return {"exams": exams, "preparing": sorted(public[i] for i in mine)}


class PrepList(APIView):
    """The exams a student can prepare for, and the ones they chose."""

    permission_classes = [IsStudent]

    def get(self, request):
        return Response(_prep_payload(request.user))

    def put(self, request):
        ids = request.data.get("category_ids")
        if not isinstance(ids, list) or not all(isinstance(i, str) for i in ids):
            raise serializers.ValidationError({"category_ids": "Send a list of exam ids."})
        found = dict(ExamCategory.objects.filter(public_id__in=ids, is_active=True).values_list("public_id", "id"))
        if set(found) != set(ids):
            raise serializers.ValidationError({"category_ids": "One of those exams does not exist."})
        wanted = set(found.values())
        with transaction.atomic():
            Preparation.objects.filter(student=request.user).exclude(category_id__in=wanted).delete()
            have = set(Preparation.objects.filter(student=request.user).values_list("category_id", flat=True))
            Preparation.objects.bulk_create(Preparation(student=request.user, category_id=i) for i in wanted - have)
        return Response(_prep_payload(request.user))


class PrepHub(APIView):
    """One exam (for example SSC) with its tests grouped as full tests, subject tests and previous year papers."""

    permission_classes = [IsStudent]

    def get(self, request, pid):
        user, now = request.user, timezone.now()
        cat = get_object_or_404(ExamCategory.objects.prefetch_related("subjects"), public_id=pid, is_active=True)
        qs = svc.visible_exams(user).filter(category=cat).select_related("category", "subject").with_totals()
        active, used = _context(user)
        cards = [exam_card(request, e, user, now, active, used) for e in qs.order_by("title")]

        full = [c for c in cards if c["test_type"] == Exam.TestType.FULL]
        previous = sorted(
            (c for c in cards if c["test_type"] == Exam.TestType.PREVIOUS_YEAR), key=lambda c: (-(c["year"] or 0), c["title"])
        )
        by_subject: dict[int | None, list] = defaultdict(list)
        for c in cards:
            if c["test_type"] == Exam.TestType.SECTIONAL:
                by_subject[c["subject"]].append(c)
        names = {s.id: s.name for s in cat.subjects.all()}
        for c in cards:
            if c["subject"] and c["subject"] not in names:
                names[c["subject"]] = c["subject_name"]
        order = [s.id for s in cat.subjects.all()] + sorted((i for i in by_subject if i not in {s.id for s in cat.subjects.all()}), key=lambda i: names.get(i) or "")
        subject_tests = [{"subject": i, "subject_name": names.get(i, "General"), "tests": by_subject.get(i, [])} for i in order]
        subject_tests.sort(key=lambda g: not g["tests"])  # subjects with tests first, upcoming ones (no tests yet) last

        tried = sum(1 for c in cards if c["attempts_used"] or c["status"] == "in_progress")
        return Response({
            "exam": {"id": cat.public_id, "name": cat.name, "description": cat.description,
                     "subjects": [{"id": s.id, "name": s.name} for s in cat.subjects.all()]},
            "preparing": Preparation.objects.filter(student=user, category=cat).exists(),
            "full_tests": full,
            "subject_tests": subject_tests,
            "previous_year": previous,
            "progress": {"total": len(cards), "attempted": tried},
        })


class StudentExamDetail(APIView):
    permission_classes = [IsStudent]

    def get(self, request, pid):
        exam = get_object_or_404(svc.visible_exams(request.user, include_practice=True).select_related("category", "subject"), public_id=pid)
        active, used = _context(request.user)
        data = exam_card(request, exam, request.user, timezone.now(), active, used)
        data["instructions"] = exam.instructions
        qs = Question.objects.filter(exam_items__section__exam=exam)
        data["languages"] = [c for c, on in (("en", qs.exclude(text="").exists()), ("hi", qs.exclude(text_hi="").exists())) if on]
        data["unanswered_penalty"] = str(exam.unanswered_penalty)
        data["sections"] = [
            {"name": s.name, "question_count": s.items.count(), "duration_minutes": s.duration_minutes if exam.sectional_timing else None}
            for s in exam.sections.all()
        ]
        return Response(data)


class StartAttempt(APIView):
    permission_classes = [IsStudent]

    def post(self, request, pid):
        exam = get_object_or_404(svc.visible_exams(request.user, include_practice=True), public_id=pid)
        try:
            attempt, created = svc.start_attempt(exam, request.user)
        except svc.CannotStart as e:
            return Response({"detail": e.reason}, status=403)
        return Response({"attempt_id": attempt.public_id, "created": created}, status=201 if created else 200)


def own_attempt(request, pid) -> Attempt:
    """The attempt, only if it belongs to the signed-in student. Anyone else gets a 404, never a hint it exists."""
    return get_object_or_404(Attempt.objects.select_related("exam"), public_id=pid, student=request.user)


def clock(attempt):
    now = timezone.now()
    return {"server_now": now, "deadline": attempt.deadline, "remaining_seconds": svc.remaining_seconds(attempt, now)}


def _question_dict(request, attempt: Attempt, aq: AttemptQuestion) -> dict:
    return {
        "id": aq.id, "number": aq.order + 1, "text": aq.text, "text_hi": aq.text_hi, "image_url": _img(request, aq.image_path),
        "marks": str(aq.marks),
        "negative_marks": money(aq.negative_marks if attempt.negative_marking_enabled else 0),
        "options": [{"id": o["id"], "text": o["text"], "text_hi": o.get("text_hi", "")} for o in aq.options],
        "selected_option": aq.selected_option_id, "marked_for_review": aq.marked_for_review,
        "visited": aq.visited, "time_spent": aq.time_spent_seconds,
    }


def attempt_payload(request, attempt: Attempt) -> dict:
    """
    The attempt as the student takes it. Never contains correct answers or explanations. For a test with timed
    sections only the section that is open right now carries its questions: later sections are not revealed and
    finished ones cannot be revisited.
    """
    if attempt.status == Attempt.Status.IN_PROGRESS:
        states = svc.advance_sections(attempt)
        if timezone.now() > attempt.deadline or (states and all(s.ended_at for s in states)):
            svc.submit_attempt(attempt.id, auto=True)
            attempt = Attempt.objects.select_related("exam").get(pk=attempt.pk)
    base = {
        "id": attempt.public_id, "status": attempt.status, "attempt_number": attempt.attempt_number,
        "sectional": attempt.sectional_timing,
        "exam": {
            "id": attempt.exam.public_id, "title": attempt.exam.title, "duration_minutes": attempt.duration_minutes,
            "total_marks": str(attempt.total_marks), "negative_marking_enabled": attempt.negative_marking_enabled,
        },
        **clock(attempt),
    }
    if attempt.status == Attempt.Status.SUBMITTED:
        return {**base, **submitted_payload(attempt)}

    members: dict[int, list[AttemptQuestion]] = {}
    names: dict[int, str] = {}
    for aq in attempt.questions.all():
        members.setdefault(aq.section_order, []).append(aq)
        names[aq.section_order] = aq.section_name

    all_rows = [aq for group in members.values() for aq in group]
    base["languages"] = [c for c, on in (("en", any(a.text for a in all_rows)), ("hi", any(a.text_hi for a in all_rows))) if on]

    if not attempt.sectional_timing:
        base["sections"] = [
            {"name": names[o], "questions": [_question_dict(request, attempt, aq) for aq in members[o]]} for o in sorted(members)
        ]
        return base

    now = timezone.now()
    states = svc.advance_sections(attempt, now)
    active = svc.active_section(states)
    sections = []
    for st in states:
        group = members.get(st.order, [])
        entry = {
            "name": st.name, "order": st.order, "duration_seconds": st.duration_seconds,
            "question_count": len(group), "answered_count": sum(1 for a in group if a.selected_option_id is not None),
            "status": "done" if st.ended_at else ("active" if st is active else "upcoming"),
            "closed_by": st.closed_by or None, "questions": [],
        }
        if st is active:
            entry["remaining_seconds"] = min(svc.section_remaining_seconds(st, now), svc.remaining_seconds(attempt, now))
            entry["questions"] = [_question_dict(request, attempt, aq) for aq in group]
        sections.append(entry)
    base["sections"] = sections
    base["active_section"] = active.order if active else None
    return base


class AttemptDetail(APIView):
    permission_classes = [IsStudent]

    def get(self, request, pid):
        return Response(attempt_payload(request, own_attempt(request, pid)))


class SubmitSection(APIView):
    """Submit the section the student is in and move on to the next one."""

    permission_classes = [IsStudent]

    def post(self, request, pid):
        attempt = own_attempt(request, pid)
        if not attempt.sectional_timing:
            return Response({"detail": "This test does not have timed sections."}, status=400)
        order = request.data.get("order")
        if not isinstance(order, int):
            raise serializers.ValidationError({"order": "Send the number of the section you are submitting."})
        updated, moved = svc.submit_section(attempt.id, order)
        updated = Attempt.objects.select_related("exam").get(pk=updated.pk)
        return Response({**attempt_payload(request, updated), "moved": moved})


def submitted_payload(attempt: Attempt) -> dict:
    visible = svc.result_visible(attempt.exam)
    return {
        "submitted_at": attempt.submitted_at, "auto_submitted": attempt.auto_submitted,
        "result_available": visible,
        "result": svc.summary(attempt) if visible else None,
    }


class AnswerItem(serializers.Serializer):
    question = serializers.IntegerField()
    selected_option = serializers.IntegerField(allow_null=True, required=False)
    marked_for_review = serializers.BooleanField(required=False)
    visited = serializers.BooleanField(required=False)
    # Running total of seconds on this question (not a delta), so retries and reordering are harmless.
    time_spent = serializers.IntegerField(required=False, min_value=0, max_value=86400)


class SaveAnswers(APIView):
    permission_classes = [IsStudent]

    def put(self, request, pid):
        attempt = own_attempt(request, pid)
        now = timezone.now()
        if attempt.status != Attempt.Status.IN_PROGRESS:
            return Response({"detail": "This attempt has already been submitted.", "status": attempt.status}, status=409)
        if now > attempt.deadline + timedelta(seconds=svc.GRACE_SECONDS):
            done = svc.submit_attempt(attempt.id, auto=True)
            return Response({"detail": "Time is up. Your attempt was submitted.", "status": done.status}, status=409)

        payload = request.data.get("answers", [request.data] if "question" in request.data else [])
        ser = AnswerItem(data=payload, many=True)
        ser.is_valid(raise_exception=True)
        rows = {aq.id: aq for aq in attempt.questions.all()}
        open_orders = None
        if attempt.sectional_timing:
            states = svc.advance_sections(attempt, now)
            if states and all(s.ended_at for s in states):
                done = svc.submit_attempt(attempt.id, auto=True)
                return Response({"detail": "Time is up. Your attempt was submitted.", "status": done.status}, status=409)
            open_orders = svc.open_section_orders(states, now)
        ignored = 0
        # Time cannot add up to more than the attempt has actually been running (plus a little network slack).
        budget = max(0, (now - attempt.started_at).total_seconds() + svc.GRACE_SECONDS) - sum(r.time_spent_seconds for r in rows.values())
        changed = []
        for item in ser.validated_data:
            aq = rows.get(item["question"])
            if aq is None:
                raise serializers.ValidationError({"question": f"Question {item['question']} is not part of this attempt."})
            if open_orders is not None and aq.section_order not in open_orders:
                ignored += 1  # that section is closed, and its answers can no longer change
                continue
            if "selected_option" in item:
                sel = item["selected_option"]
                if sel is not None and sel not in {o["id"] for o in aq.options}:
                    raise serializers.ValidationError({"selected_option": "That option does not belong to this question."})
                aq.selected_option_id = sel
                aq.answered_at = now if sel is not None else None
            if "marked_for_review" in item:
                aq.marked_for_review = item["marked_for_review"]
            if "visited" in item:
                aq.visited = item["visited"] or aq.visited
            if "time_spent" in item and item["time_spent"] > aq.time_spent_seconds:
                extra = min(item["time_spent"] - aq.time_spent_seconds, max(0, int(budget)))
                aq.time_spent_seconds += extra
                budget -= extra
            changed.append(aq)
        AttemptQuestion.objects.bulk_update(
            changed, ["selected_option_id", "answered_at", "marked_for_review", "visited", "time_spent_seconds"]
        )
        return Response({"saved": len(changed), "ignored": ignored, **clock(attempt)})


class SubmitAttempt(APIView):
    permission_classes = [IsStudent]

    def post(self, request, pid):
        attempt = own_attempt(request, pid)
        auto = timezone.now() > attempt.deadline
        if attempt.sectional_timing and not auto and attempt.status == Attempt.Status.IN_PROGRESS:
            states = svc.advance_sections(attempt)
            active = svc.active_section(states)
            if active is not None and active is not states[-1]:
                return Response({"detail": "Submit or finish the current section first. Sections open in order."}, status=409)
        done = svc.submit_attempt(attempt.id, auto=auto)
        done = Attempt.objects.select_related("exam").get(pk=done.pk)
        return Response({"id": done.public_id, "status": done.status, **submitted_payload(done)})


class AttemptResult(APIView):
    """Detailed result for the student's own submitted attempt, once the test's release policy allows it."""

    permission_classes = [IsStudent]

    def get(self, request, pid):
        attempt = own_attempt(request, pid)
        if attempt.status != Attempt.Status.SUBMITTED:
            return Response({"detail": "Submit the attempt to see its result."}, status=409)
        if not svc.result_visible(attempt.exam):
            return Response({"detail": "Your result has not been released yet.", "result_available": False}, status=403)
        reveal = svc.solutions_visible(attempt.exam)
        data = build_result(attempt, request, reveal_solutions=reveal)
        if reveal:
            from .saved_views import annotate_result

            annotate_result(attempt, data)
        return Response(data)


class AttemptHistory(APIView):
    permission_classes = [IsStudent]

    def get(self, request):
        svc.expire_overdue(Attempt.objects.filter(student=request.user))
        out = []
        for a in Attempt.objects.filter(student=request.user).select_related("exam"):
            visible = a.status == Attempt.Status.SUBMITTED and svc.result_visible(a.exam)
            out.append({
                "id": a.public_id, "exam": a.exam.public_id, "exam_title": a.exam.title, "attempt_number": a.attempt_number,
                "status": a.status, "started_at": a.started_at, "submitted_at": a.submitted_at,
                "auto_submitted": a.auto_submitted, "result_available": visible,
                "score": str(a.score) if visible else None, "total_marks": str(a.total_marks),
                "passed": a.passed if visible else None,
                "percentage": round(float(a.score) / float(a.total_marks) * 100, 1) if visible and a.total_marks else None,
            })
        return Response(out)


class StudentDashboard(APIView):
    """Everything is computed from the signed-in student's own attempts. Unreleased results are excluded."""

    permission_classes = [IsStudent]

    def get(self, request):
        user, now = request.user, timezone.now()
        active, used = _context(user)
        cards = [exam_card(request, e, user, now, active, used) for e in svc.visible_exams(user).select_related("category", "subject").with_totals()]
        counts = defaultdict(int)
        for c in cards:
            counts[c["status"]] += 1

        attempts = list(
            Attempt.objects.filter(student=user, status=Attempt.Status.SUBMITTED).select_related("exam").order_by("-submitted_at")
        )
        released = [a for a in attempts if svc.result_visible(a.exam, now)]
        pct = [float(a.score) / float(a.total_marks) * 100 for a in released if a.total_marks]
        total_correct = sum(a.correct_count for a in released)
        total_answered = sum(a.correct_count + a.incorrect_count for a in released)

        subjects: dict[str, dict] = defaultdict(lambda: {"correct": 0, "answered": 0, "marks": Decimal("0"), "max": Decimal("0")})
        rows = AttemptQuestion.objects.filter(attempt__in=[a.id for a in released])
        for r in rows.values("subject_name", "is_correct", "marks_awarded", "marks"):
            s = subjects[r["subject_name"] or "General"]
            s["max"] += r["marks"]
            s["marks"] += r["marks_awarded"] or 0
            if r["is_correct"] is not None:
                s["answered"] += 1
                s["correct"] += 1 if r["is_correct"] else 0

        attempted_exam_ids = {a.exam_id for a in attempts}
        mine = list(Preparation.objects.filter(student=user).select_related("category"))
        totals = dict(
            svc.visible_exams(user).order_by().filter(category__in=[p.category_id for p in mine])
            .values_list("category").annotate(n=Count("id", distinct=True))
        )
        tried = dict(
            Attempt.objects.filter(student=user, status=Attempt.Status.SUBMITTED, exam__category__in=[p.category_id for p in mine])
            .values_list("exam__category").annotate(n=Count("exam", distinct=True))
        )
        return Response({
            "preparing": [
                {"id": p.category.public_id, "name": p.category.name, "total_tests": totals.get(p.category_id, 0),
                 "attempted_tests": tried.get(p.category_id, 0)}
                for p in mine if p.category.is_active
            ],
            "counts": {
                "available": counts["available"], "upcoming": counts["upcoming"],
                "in_progress": counts["in_progress"], "completed": len(attempts),
            },
            "average_score_percent": round(sum(pct) / len(pct), 1) if pct else None,
            "accuracy_percent": round(total_correct * 100 / total_answered, 1) if total_answered else None,
            "in_progress": [c for c in cards if c["status"] == "in_progress"],
            "upcoming": [c for c in cards if c["status"] == "upcoming"],
            "recent_results": [
                {"id": a.public_id, "exam_title": a.exam.title, "score": str(a.score), "total_marks": str(a.total_marks),
                 "passed": a.passed, "submitted_at": a.submitted_at}
                for a in released[:5]
            ],
            "trend": [
                {"attempt": a.public_id, "title": a.exam.title, "percentage": round(float(a.score) / float(a.total_marks) * 100, 1),
                 "submitted_at": a.submitted_at}
                for a in reversed(released[:10]) if a.total_marks
            ],
            "subjects": [
                {"subject": name, "accuracy": round(v["correct"] * 100 / v["answered"], 1) if v["answered"] else 0,
                 "marks": str(v["marks"]), "max_marks": str(v["max"])}
                for name, v in sorted(subjects.items())
            ],
            "activity": activity.summary(user),
            "recommended": [c for c in cards if c["status"] == "available" and c["id"] not in attempted_exam_ids][:4],
        })
