import re
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
from core.media import media_url
from core.numbers import money
from exams.models import Exam, ExamCategory, Preparation
from exams.tabs import custom_keys, resolve_tabs
from questionbank.models import Question

from . import services as svc
from .models import Attempt, AttemptQuestion, AttemptSection
from . import activity
from .results import build_result


def _img(request, path):
    return media_url(request, path)


def _last_submitted(request, user) -> dict:
    """The student's most recent submitted attempt for every test, worked out once per request."""
    if not hasattr(request, "_last_submitted"):
        latest = {}
        for exam_id, pid in Attempt.objects.filter(student=user, status=Attempt.Status.SUBMITTED).order_by("-id").values_list("exam_id", "public_id"):
            latest.setdefault(exam_id, pid)  # newest first, so the first one seen is the latest
        request._last_submitted = latest
    return request._last_submitted


def natural_key(title):
    """Sort key that puts "Mock 2" before "Mock 10" (numbers compared as numbers, case ignored)."""
    return [int(t) if t.isdigit() else t for t in re.split(r"(\d+)", (title or "").lower())]


def by_title(cards):
    """Test cards in natural title order, so a series reads 1, 2, 3 ... 10 whatever order they were created or stored in."""
    return sorted(cards, key=lambda c: (natural_key(c["title"]), c["id"]))


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
        "also_in": [c.public_id for c in exam.also_in.all()],  # other exams that show this test too
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
        "is_live": exam.is_live,
        "join_closes_at": exam.start_at + timedelta(minutes=exam.late_join_minutes) if exam.is_live and exam.start_at else None,
        "status": status,
        "can_start": can_start,
        "reason": reason,
        "active_attempt_id": active.public_id if active else None,
        # Where a student who has already taken this test can look at the result.
        "last_attempt_id": (last := _last_submitted(request, user).get(exam.id)),
        "result_available": bool(last) and svc.result_visible(exam, now),
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
        qs = svc.visible_exams(user).select_related("category", "subject").prefetch_related("also_in").with_totals()
        if c := request.query_params.get("category"):
            qs = qs.filter(Q(category__public_id=c) | Q(also_in__public_id=c)).distinct()
        if s := request.query_params.get("subject"):
            qs = qs.filter(sections__items__question__subject_id=s).distinct()
        if t := request.query_params.get("test_type"):
            qs = qs.filter(test_type=t)
        if q := request.query_params.get("search"):
            qs = qs.filter(Q(title__icontains=q) | Q(description__icontains=q))
        if request.query_params.get("live"):
            qs = qs.filter(is_live=True, end_at__gte=now)  # live tests that have not finished yet
        active, used = _context(user)
        cards = [exam_card(request, e, user, now, active, used) for e in qs.order_by()]
        # Tests with a start time come first, soonest first; the rest follow in natural title order (the database would put the
        # ones without a start time first or last depending on which database it is, which is why production differed from local).
        cards.sort(key=lambda c: (c["start_at"] is None, c["start_at"] or "", natural_key(c["title"]), c["id"]))
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
                {"id": c.public_id, "name": c.name, "group": {"id": c.group.public_id, "name": c.group.name} if c.group and c.group.is_active else None}
                for c in ExamCategory.objects.filter(Q(exams__in=visible) | Q(shared_exams__in=visible))
                .select_related("group").distinct().order_by("group__order", "group__name", "name")
            ],
            "subjects": list(Subject.objects.filter(questions__exam_items__section__exam__in=visible).distinct().order_by("name").values("id", "name")),
        })


def _prep_payload(user) -> dict:
    visible = svc.visible_exams(user).order_by()
    counts: dict[int, dict[str, int]] = defaultdict(lambda: defaultdict(int))
    rows = visible.exclude(category=None).values("category", "test_type").annotate(n=Count("id", distinct=True))
    for r in rows:
        counts[r["category"]][r["test_type"]] += r["n"]
    # A test shared with other exams counts in each of them.
    for r in visible.filter(also_in__isnull=False).values("also_in", "test_type").annotate(n=Count("id", distinct=True)):
        counts[r["also_in"]][r["test_type"]] += r["n"]
    mine = set(Preparation.objects.filter(student=user).values_list("category_id", flat=True))
    public = dict(ExamCategory.objects.values_list("id", "public_id"))
    exams = []
    families = {}
    for c in ExamCategory.objects.filter(is_active=True).select_related("group").prefetch_related("subjects").order_by("group__order", "group__name", "name"):
        if c.id not in counts and c.id not in mine:
            continue  # nothing to prepare with yet
        k = counts[c.id]
        group = c.group if c.group and c.group.is_active else None
        if group:
            families[group.id] = {"id": group.public_id, "name": group.name, "description": group.description, "order": group.order}
        exams.append({
            "id": c.public_id, "name": c.name, "description": c.description,
            "group": {"id": group.public_id, "name": group.name} if group else None,
            "subjects": [s.name for s in c.subjects.all()],
            "counts": {"full_test": k["full_test"], "mini_mock": k["mini_mock"], "sectional": k["sectional"], "previous_year": k["previous_year"],
                       "total": sum(k.values())},
            "preparing": c.id in mine,
        })
    return {
        "exams": exams, "preparing": sorted(public[i] for i in mine),
        "families": sorted(families.values(), key=lambda f: (f["order"], f["name"])),
    }


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
        cat = get_object_or_404(ExamCategory.objects.select_related("group").prefetch_related("subjects"), public_id=pid, is_active=True)
        qs = svc.visible_exams(user).in_category(cat).select_related("category", "subject").prefetch_related("also_in").with_totals()
        active, used = _context(user)
        exams = list(qs.order_by("title"))
        cards = [exam_card(request, e, user, now, active, used) for e in exams]

        # A test the admin put in one of this exam's own tabs shows there instead of in the tab of its type. A test that is only
        # shared into this exam (its own exam is another one) always goes by type, because the tab belongs to its own exam.
        own_tabs = custom_keys(cat.tabs)
        custom: dict[str, list] = {k: [] for k in own_tabs}
        by_type = []
        for e, c in zip(exams, cards):
            if e.category_id == cat.id and e.tab_key in own_tabs:
                custom[e.tab_key].append(c)
            else:
                by_type.append(c)

        full = by_title(c for c in by_type if c["test_type"] == Exam.TestType.FULL)
        mini = by_title(c for c in by_type if c["test_type"] == Exam.TestType.MINI_MOCK)
        previous = sorted(
            (c for c in by_type if c["test_type"] == Exam.TestType.PREVIOUS_YEAR), key=lambda c: (-(c["year"] or 0), natural_key(c["title"]))
        )
        by_subject: dict[int | None, list] = defaultdict(list)
        for c in by_type:
            if c["test_type"] == Exam.TestType.SECTIONAL:
                by_subject[c["subject"]].append(c)
        names = {s.id: s.name for s in cat.subjects.all()}
        for c in cards:
            if c["subject"] and c["subject"] not in names:
                names[c["subject"]] = c["subject_name"]
        order = [s.id for s in cat.subjects.all()] + sorted((i for i in by_subject if i not in {s.id for s in cat.subjects.all()}), key=lambda i: names.get(i) or "")
        subject_tests = [{"subject": i, "subject_name": names.get(i, "General"), "tests": by_title(by_subject.get(i, []))} for i in order]
        subject_tests.sort(key=lambda g: not g["tests"])  # subjects with tests first, upcoming ones (no tests yet) last

        tried = sum(1 for c in cards if c["attempts_used"] or c["status"] == "in_progress")
        return Response({
            "exam": {"id": cat.public_id, "name": cat.name, "description": cat.description,
                     "group": {"id": cat.group.public_id, "name": cat.group.name} if cat.group else None,
                     "subjects": [{"id": s.id, "name": s.name} for s in cat.subjects.all()]},
            "preparing": Preparation.objects.filter(student=user, category=cat).exists(),
            "tabs": resolve_tabs(cat.tabs),
            "custom_tests": {k: by_title(v) for k, v in custom.items()},
            "full_tests": full,
            "mini_mocks": mini,
            "subject_tests": subject_tests,
            "previous_year": previous,
            "progress": {"total": len(cards), "attempted": tried},
        })


class StudentLiveExam(APIView):
    """
    The page behind a live test's share link: when it starts, how long it runs and where it stands, by the server's clock. Only for a
    signed-in student who is allowed to take the test; anyone else gets nothing, as for any test they cannot see. Never the questions.
    """

    permission_classes = [IsStudent]

    def get(self, request, pid):
        exam = get_object_or_404(
            svc.visible_exams(request.user).filter(is_live=True).select_related("category__group", "subject"), public_id=pid
        )
        now = timezone.now()
        active, used = _context(request.user)
        card = exam_card(request, exam, request.user, now, active, used)
        closes = exam.start_at + timedelta(minutes=exam.late_join_minutes)
        if now < exam.start_at:
            state = "upcoming"
        elif now <= closes:
            state = "live"
        elif exam.end_at and now <= exam.end_at:
            state = "joining_closed"
        else:
            state = "ended"
        group = exam.category.group if exam.category and exam.category.group and exam.category.group.is_active else None
        response = Response({
            **card,
            "family": group.name if group else None,
            "ends_at": exam.end_at, "late_join_minutes": exam.late_join_minutes,
            "state": state, "server_now": now,
        })
        response["Cache-Control"] = "no-store"  # a countdown must never come from a cache
        return response


class StudentExamDetail(APIView):
    permission_classes = [IsStudent]

    def get(self, request, pid):
        exam = get_object_or_404(svc.visible_exams(request.user, include_practice=True).select_related("category", "subject"), public_id=pid)
        active, used = _context(request.user)
        data = exam_card(request, exam, request.user, timezone.now(), active, used)
        data["instructions"] = exam.instructions
        data["server_now"] = timezone.now()  # lets a live test count down to its start by the server clock
        data["late_join_minutes"] = exam.late_join_minutes
        qs = Question.objects.filter(exam_items__section__exam=exam)
        data["languages"] = [c for c, on in (("en", qs.exclude(text="").exists()), ("hi", qs.exclude(text_hi="").exists())) if on]
        data["unanswered_penalty"] = str(exam.unanswered_penalty)
        data["allow_pause"], data["max_pauses"], data["pause_minutes"] = exam.allow_pause, exam.max_pauses, exam.pause_minutes
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


def pause_info(attempt: Attempt) -> dict:
    return {
        "allowed": attempt.allow_pause, "paused": attempt.paused_at is not None, "paused_at": attempt.paused_at,
        "auto_resume_at": svc.pause_ends_at(attempt), "max_minutes": attempt.pause_minutes,
        "pauses_used": attempt.pause_count, "pauses_left": svc.pauses_left(attempt),
    }


def clock(attempt):
    now = timezone.now()
    return {
        "server_now": now, "deadline": attempt.deadline, "remaining_seconds": svc.remaining_seconds(attempt, now),
        "pause": pause_info(attempt),
    }


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
        attempt = svc.settle_pause(attempt)  # a pause that ran out of time has already ended
        if attempt.paused_at is None:
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
    if attempt.paused_at is not None:
        # Nothing of the test is shown while it is paused.
        return {**base, "paused": True, "languages": [], "sections": [], "active_section": None}

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
        attempt = svc.settle_pause(own_attempt(request, pid))
        if attempt.paused_at is not None:
            return Response({"detail": "The test is paused. Resume it to continue.", "paused": True}, status=409)
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
        attempt = svc.settle_pause(attempt)
        if attempt.paused_at is not None:
            return Response({"detail": "The test is paused. Resume it to continue.", "paused": True, "status": attempt.status}, status=409)
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
        budget = max(0, (now - attempt.started_at).total_seconds() - attempt.paused_seconds + svc.GRACE_SECONDS) - sum(r.time_spent_seconds for r in rows.values())
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
        attempt = svc.settle_pause(own_attempt(request, pid))
        if attempt.status == Attempt.Status.IN_PROGRESS and attempt.paused_at is not None:
            attempt = svc.resume_attempt(attempt.id)  # submitting ends the pause; the paused time is not counted
        auto = timezone.now() > attempt.deadline
        if attempt.sectional_timing and not auto and attempt.status == Attempt.Status.IN_PROGRESS:
            states = svc.advance_sections(attempt)
            active = svc.active_section(states)
            if active is not None and active is not states[-1]:
                return Response({"detail": "Submit or finish the current section first. Sections open in order."}, status=409)
        done = svc.submit_attempt(attempt.id, auto=auto)
        done = Attempt.objects.select_related("exam").get(pk=done.pk)
        return Response({"id": done.public_id, "status": done.status, **submitted_payload(done)})


class PauseAttempt(APIView):
    """Stops the clock and hides the test until the student resumes. Idempotent."""

    permission_classes = [IsStudent]

    def post(self, request, pid):
        attempt = own_attempt(request, pid)
        try:
            updated = svc.pause_attempt(attempt.id)
        except svc.CannotPause as e:
            return Response({"detail": e.reason}, status=409)
        updated = Attempt.objects.select_related("exam").get(pk=updated.pk)
        return Response(attempt_payload(request, updated))


class ResumeAttempt(APIView):
    """Restarts the clock with the same time left and returns the test as the student left it."""

    permission_classes = [IsStudent]

    def post(self, request, pid):
        attempt = own_attempt(request, pid)
        if attempt.status != Attempt.Status.IN_PROGRESS:
            return Response({"detail": "This attempt has already been submitted.", "status": attempt.status}, status=409)
        updated = svc.resume_attempt(attempt.id)
        updated = Attempt.objects.select_related("exam").get(pk=updated.pk)
        return Response(attempt_payload(request, updated))


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
        mine = list(Preparation.objects.filter(student=user).select_related("category__group"))
        totals, tried = {}, {}
        for p in mine:
            in_exam = svc.visible_exams(user).order_by().in_category(p.category)
            totals[p.category_id] = in_exam.count()
            tried[p.category_id] = (
                Attempt.objects.filter(student=user, status=Attempt.Status.SUBMITTED, exam__in=in_exam).values("exam").distinct().count()
            )
        return Response({
            "preparing": [
                {"id": p.category.public_id, "name": p.category.name, "total_tests": totals.get(p.category_id, 0),
                 "attempted_tests": tried.get(p.category_id, 0),
                 "group": {"id": p.category.group.public_id, "name": p.category.group.name} if p.category.group and p.category.group.is_active else None}
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
