from django.shortcuts import get_object_or_404
from django.utils import timezone
from rest_framework.response import Response
from rest_framework.views import APIView

from accounts.permissions import IsStudent
from attempts import services as svc
from attempts.models import Attempt
from attempts.views import _context, exam_card

from .models import StudyNote, TestSeries


def note_summary(n: StudyNote) -> dict:
    body = n.body or n.body_hi
    return {
        "id": n.public_id, "title": n.title, "title_hi": n.title_hi, "category": n.category.name if n.category else None,
        "subject": n.subject.name if n.subject else None, "note_date": n.note_date, "updated_at": n.updated_at,
        "summary": (body[:200] + "…") if len(body) > 200 else body,
    }


class NoteList(APIView):
    permission_classes = [IsStudent]

    def get(self, request):
        qs = StudyNote.objects.filter(is_published=True).select_related("category", "subject")
        if cat := request.query_params.get("category"):
            qs = qs.filter(category__public_id=cat)
        if sub := request.query_params.get("subject"):
            qs = qs.filter(subject_id=sub) if sub.isdigit() else qs.filter(subject__name__iexact=sub)
        if q := (request.query_params.get("search") or "").strip():
            qs = qs.filter(title__icontains=q) | qs.filter(title_hi__icontains=q) | qs.filter(body__icontains=q) | qs.filter(body_hi__icontains=q)
        return Response({"results": [note_summary(n) for n in qs.distinct()[:200]]})


class NoteDetail(APIView):
    permission_classes = [IsStudent]

    def get(self, request, pid):
        n = get_object_or_404(StudyNote.objects.select_related("category", "subject"), public_id=pid, is_published=True)
        return Response({**note_summary(n), "body": n.body, "body_hi": n.body_hi})


class SeriesList(APIView):
    """The published series, each with how many of its tests this student has attempted."""

    permission_classes = [IsStudent]

    def get(self, request):
        visible = set(svc.visible_exams(request.user).values_list("id", flat=True))
        done = set(
            Attempt.objects.filter(student=request.user, status=Attempt.Status.SUBMITTED).values_list("exam_id", flat=True)
        )
        out = []
        for s in TestSeries.objects.filter(is_published=True).select_related("category").prefetch_related("items"):
            ids = [i.exam_id for i in s.items.all() if i.exam_id in visible]
            if not ids:
                continue
            out.append({
                "id": s.public_id, "title": s.title, "description": s.description,
                "category": s.category.name if s.category else None, "total": len(ids), "attempted": sum(1 for i in ids if i in done),
            })
        return Response({"results": out})


class SeriesDetail(APIView):
    permission_classes = [IsStudent]

    def get(self, request, pid):
        series = get_object_or_404(TestSeries.objects.select_related("category"), public_id=pid, is_published=True)
        user, now = request.user, timezone.now()
        active, used = _context(user)
        order = [i.exam_id for i in series.items.all()]
        exams = {e.id: e for e in svc.visible_exams(user).filter(pk__in=order).select_related("category", "subject").with_totals()}
        cards = [exam_card(request, exams[i], user, now, active, used) for i in order if i in exams]
        return Response({
            "id": series.public_id, "title": series.title, "description": series.description,
            "category": series.category.name if series.category else None,
            "total": len(cards), "attempted": sum(1 for c in cards if c["attempts_used"] > 0),
            "tests": cards,
        })
