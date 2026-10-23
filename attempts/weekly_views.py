from rest_framework.response import Response
from rest_framework.views import APIView

from accounts.permissions import IsStudent

from . import weekly


class WeeklySummary(APIView):
    """GET: the student's last seven days compared with the seven before."""

    permission_classes = [IsStudent]

    def get(self, request):
        return Response(weekly.summary(request.user))


class Revise(APIView):
    """GET ?source=saved|mistakes&limit=: revision cards, shuffled. `total` is how many exist before the limit."""

    permission_classes = [IsStudent]

    def get(self, request):
        from rest_framework import serializers as s

        from . import revise

        source = request.query_params.get("source", "mistakes")
        if source not in ("saved", "mistakes"):
            raise s.ValidationError({"source": "Use saved or mistakes."})
        try:
            limit = int(request.query_params.get("limit", revise.DEFAULT_LIMIT))
        except ValueError:
            raise s.ValidationError({"limit": "Give a whole number."})
        limit = max(1, min(limit, revise.MAX_LIMIT))
        total, cards = revise.cards(request.user, source, limit)
        return Response({"source": source, "total": total, "cards": cards})
