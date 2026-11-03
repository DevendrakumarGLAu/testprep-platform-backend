from rest_framework import serializers, viewsets
from rest_framework.decorators import action
from rest_framework.response import Response

from accounts.models import log_activity
from accounts.permissions import IsAdminOrSuperAdmin
from core.media import prepare_image

from .models import Popup, StudyNote, TestSeries
from .serializers import PopupSerializer, SeriesSerializer, StudyNoteSerializer


MAX_IMAGE_BYTES = 2 * 1024 * 1024


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


class PopupViewSet(viewsets.ModelViewSet):
    serializer_class = PopupSerializer
    permission_classes = [IsAdminOrSuperAdmin]
    queryset = Popup.objects.select_related("category", "batch")

    def perform_create(self, serializer):
        popup = serializer.save(created_by=self.request.user)
        log_activity(self.request.user, "popup.create", popup)

    def perform_update(self, serializer):
        log_activity(self.request.user, "popup.update", serializer.save())

    def perform_destroy(self, instance):
        log_activity(self.request.user, "popup.delete", None, instance.title)
        instance.delete()

    @action(detail=True, methods=["post", "delete"], url_path="image")
    def image(self, request, pk=None):
        popup = self.get_object()
        if request.method == "DELETE":
            self._drop_image(popup)
            popup.image = None
            popup.save(update_fields=["image"])
            return Response(self.get_serializer(popup).data)
        f = request.FILES.get("image")
        if not f:
            raise serializers.ValidationError({"image": "No file uploaded."})
        if f.size > MAX_IMAGE_BYTES:
            raise serializers.ValidationError({"image": "Image must be 2 MB or smaller."})
        if not (f.content_type or "").startswith("image/"):
            raise serializers.ValidationError({"image": "File must be an image."})
        stored = prepare_image(f)
        self._drop_image(popup)
        popup.image = stored
        popup.save()
        return Response(self.get_serializer(popup).data)

    @staticmethod
    def _drop_image(popup):
        if popup.image:
            popup.image.storage.delete(popup.image.name)
