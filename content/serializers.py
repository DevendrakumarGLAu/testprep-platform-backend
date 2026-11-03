from django.db import transaction
from rest_framework import serializers

from core.media import media_url
from exams.models import Exam

from .models import Popup, SeriesItem, StudyNote, TestSeries


class StudyNoteSerializer(serializers.ModelSerializer):
    category_name = serializers.CharField(source="category.name", read_only=True, default=None)
    subject_name = serializers.CharField(source="subject.name", read_only=True, default=None)

    class Meta:
        model = StudyNote
        fields = [
            "id", "public_id", "title", "title_hi", "body", "body_hi", "category", "category_name", "subject", "subject_name",
            "note_date", "is_published", "created_at", "updated_at",
        ]
        read_only_fields = ["id", "public_id", "created_at", "updated_at"]

    def validate(self, attrs):
        get = lambda f: attrs.get(f, getattr(self.instance, f, ""))  # noqa: E731
        if not (get("title").strip() or get("title_hi").strip()):
            raise serializers.ValidationError({"title": "Give the note a title in English, Hindi or both."})
        if not (get("body").strip() or get("body_hi").strip()):
            raise serializers.ValidationError({"body": "Write the note in English, Hindi or both."})
        return attrs


class SeriesSerializer(serializers.ModelSerializer):
    category_name = serializers.CharField(source="category.name", read_only=True, default=None)
    exam_ids = serializers.ListField(child=serializers.IntegerField(), write_only=True, required=False, max_length=100)
    exams = serializers.SerializerMethodField()

    class Meta:
        model = TestSeries
        fields = ["id", "public_id", "title", "description", "category", "category_name", "is_published", "exam_ids", "exams", "created_at"]
        read_only_fields = ["id", "public_id", "created_at"]

    def get_exams(self, obj):
        return [{"id": i.exam_id, "title": i.exam.title, "status": i.exam.status} for i in obj.items.all()]

    def validate_exam_ids(self, ids):
        if len(set(ids)) != len(ids):
            raise serializers.ValidationError("A test can appear in a series only once.")
        found = {e.id: e for e in Exam.objects.filter(pk__in=ids, is_practice=False).exclude(status=Exam.Status.ARCHIVED)}
        missing = [i for i in ids if i not in found]
        if missing:
            raise serializers.ValidationError(f"Unknown or archived test(s): {missing}.")
        return ids

    def _set_items(self, series, ids):
        series.items.all().delete()
        SeriesItem.objects.bulk_create(SeriesItem(series=series, exam_id=i, order=n) for n, i in enumerate(ids))

    @transaction.atomic
    def create(self, validated_data):
        ids = validated_data.pop("exam_ids", [])
        series = TestSeries.objects.create(**validated_data)
        self._set_items(series, ids)
        return series

    @transaction.atomic
    def update(self, instance, validated_data):
        ids = validated_data.pop("exam_ids", None)
        for k, v in validated_data.items():
            setattr(instance, k, v)
        instance.save()
        if ids is not None:
            self._set_items(instance, ids)
        return instance


class PopupSerializer(serializers.ModelSerializer):
    category_name = serializers.CharField(source="category.name", read_only=True, default=None)
    batch_name = serializers.CharField(source="batch.name", read_only=True, default=None)
    seen_count = serializers.SerializerMethodField()
    audience_size = serializers.SerializerMethodField()
    state = serializers.SerializerMethodField()
    image_url = serializers.SerializerMethodField()

    class Meta:
        model = Popup
        fields = [
            "id", "title", "body", "image_url", "link", "button_label", "audience", "category", "category_name", "batch", "batch_name",
            "starts_at", "ends_at", "is_active", "seen_count", "audience_size", "state", "created_at",
        ]
        read_only_fields = ["id", "created_at"]

    def get_image_url(self, obj):
        return media_url(self.context.get("request"), obj.image.name) if obj.image else None

    def get_seen_count(self, obj):
        return obj.seen.count()

    def get_audience_size(self, obj):
        from .popups import audience_of

        return audience_of(obj).count()

    def get_state(self, obj):
        from .popups import state_of

        return state_of(obj)

    def validate_title(self, value):
        value = value.strip()
        if not value:
            raise serializers.ValidationError("Give the pop-up a title.")
        return value

    def validate_link(self, value):
        value = value.strip()
        if value and not (value.startswith("/") and not value.startswith("//")) and not value.startswith("https://"):
            raise serializers.ValidationError("Use a page of this app, such as /student/plan, or a full https:// address.")
        return value

    def validate(self, attrs):
        get = lambda f, default=None: attrs.get(f, getattr(self.instance, f, default))  # noqa: E731
        audience = get("audience", Popup.Audience.ALL)
        if audience == Popup.Audience.EXAM and not get("category"):
            raise serializers.ValidationError({"category": "Choose the exam."})
        if audience == Popup.Audience.BATCH and not get("batch"):
            raise serializers.ValidationError({"batch": "Choose the batch."})
        if audience != Popup.Audience.EXAM:
            attrs["category"] = None
        if audience != Popup.Audience.BATCH:
            attrs["batch"] = None
        starts, ends = get("starts_at"), get("ends_at")
        if starts and ends and ends <= starts:
            raise serializers.ValidationError({"ends_at": "The end must be after the start."})
        if get("link") and not (get("button_label") or "").strip():
            attrs["button_label"] = "Open"
        return attrs
