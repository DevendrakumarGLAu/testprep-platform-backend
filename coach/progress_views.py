from django.http import HttpResponse
from django.shortcuts import get_object_or_404
from rest_framework import serializers
from rest_framework.response import Response
from rest_framework.views import APIView

from accounts.permissions import IsAdminOrSuperAdmin
from exams.models import Batch

from . import progress


class BatchProgress(APIView):
    """GET ?days=30&csv=1: each student of a batch with their tests, average, trend and a status (behind / declining / steady / improving)."""

    permission_classes = [IsAdminOrSuperAdmin]

    def get(self, request, pk):
        batch = get_object_or_404(Batch, pk=pk)
        try:
            days = int(request.query_params.get("days", 30))
        except ValueError:
            raise serializers.ValidationError({"days": "Give a whole number of days."})
        days = max(7, min(days, 180))
        report = progress.batch_progress(batch, days)
        if request.query_params.get("csv"):
            resp = HttpResponse(progress.as_csv(report), content_type="text/csv")
            resp["Content-Disposition"] = f'attachment; filename="progress-{batch.id}.csv"'
            return resp
        return Response(report)
