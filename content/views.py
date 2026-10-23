from rest_framework import viewsets

from accounts.models import log_activity
from accounts.permissions import IsAdminOrSuperAdmin

from .models import StudyNote, TestSeries
from .serializers import SeriesSerializer, StudyNoteSerializer


class StudyNoteViewSet(viewsets.ModelViewSet):
    queryset = StudyNote.objects.select_related("category", "subject")
    serializer_class = StudyNoteSerializer
    permission_classes = [IsAdminOrSuperAdmin]
    filterset_fields = ["category", "subject", "is_published"]
    search_fields = ["title", "title_hi", "body", "body_hi"]
    ordering_fields = ["note_date", "created_at", "title"]

    def perform_create(self, serializer):
        note = serializer.save(created_by=self.request.user)
        log_activity(self.request.user, "note.create", note)

    def perform_update(self, serializer):
        log_activity(self.request.user, "note.update", serializer.save())

    def perform_destroy(self, instance):
        log_activity(self.request.user, "note.delete", None, instance.display_title)
        instance.delete()


class TestSeriesViewSet(viewsets.ModelViewSet):
    serializer_class = SeriesSerializer
    permission_classes = [IsAdminOrSuperAdmin]
    filterset_fields = ["category", "is_published"]
    search_fields = ["title", "description"]
    __test__ = False

    def get_queryset(self):
        return TestSeries.objects.select_related("category").prefetch_related("items__exam")

    def perform_create(self, serializer):
        series = serializer.save(created_by=self.request.user)
        log_activity(self.request.user, "series.create", series)

    def perform_update(self, serializer):
        log_activity(self.request.user, "series.update", serializer.save())

    def perform_destroy(self, instance):
        log_activity(self.request.user, "series.delete", None, instance.title)
        instance.delete()
