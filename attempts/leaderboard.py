"""Rank and leaderboard for one test: each student's best submitted attempt, among students whose result is released."""
from django.shortcuts import get_object_or_404
from rest_framework.response import Response
from rest_framework.views import APIView

from accounts.permissions import IsStudent

from . import services as svc
from .models import Attempt

TOP = 10


def display_name(user) -> str:
    """First name and the first letter of the last name, so a leaderboard does not publish full names."""
    first = (user.first_name or user.username).strip().split(" ")[0]
    last = (user.last_name or "").strip()
    return f"{first} {last[0]}." if last else first


def best_attempts(exam):
    """One row per student: the attempt with the highest score (the quicker one wins a tie). Returns a ranked list."""
    rows = {}
    qs = Attempt.objects.filter(exam=exam, status=Attempt.Status.SUBMITTED, score__isnull=False).select_related("student")
    for a in qs.order_by("student_id", "-score", "time_taken_seconds"):
        rows.setdefault(a.student_id, a)
    ranked = sorted(rows.values(), key=lambda a: (-a.score, a.time_taken_seconds or 10**9, a.id))
    out, last_key, last_rank = [], None, 0
    for i, a in enumerate(ranked, 1):
        key = (a.score, a.time_taken_seconds)
        rank = last_rank if key == last_key else i  # equal score and time share a rank
        out.append((rank, a))
        last_key, last_rank = key, rank
    return out


class Leaderboard(APIView):
    permission_classes = [IsStudent]

    def get(self, request, pid):
        exam = get_object_or_404(svc.visible_exams(request.user), public_id=pid)
        if not svc.result_visible(exam):
            return Response({"detail": "Results for this test have not been released yet."}, status=403)
        ranked = best_attempts(exam)
        total = len(ranked)

        def row(rank, a):
            pct = round(float(a.score) / float(a.total_marks) * 100, 1) if a.total_marks else None
            return {
                "rank": rank, "name": display_name(a.student), "score": str(a.score), "total_marks": str(a.total_marks),
                "percent": pct, "time_taken_seconds": a.time_taken_seconds, "is_me": a.student_id == request.user.id,
            }

        mine = next(((r, a) for r, a in ranked if a.student_id == request.user.id), None)
        me = None
        if mine:
            rank, a = mine
            lower = sum(1 for _, other in ranked if other.score < a.score)
            me = {**row(rank, a), "better_than_percent": round(lower * 100 / total, 1) if total else 0}
        return Response({
            "exam": exam.title, "participants": total, "me": me,
            "top": [row(r, a) for r, a in ranked[:TOP]],
        })
