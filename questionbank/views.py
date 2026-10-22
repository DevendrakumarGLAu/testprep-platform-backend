import difflib
import random

import django_filters
from django.db import transaction
from django.db.models import Count, ProtectedError
from PIL import Image
from rest_framework import serializers, status, viewsets
from rest_framework.decorators import action
from rest_framework.parsers import FormParser, JSONParser, MultiPartParser
from rest_framework.response import Response

from accounts.models import log_activity
from accounts.permissions import IsAdminOrSuperAdmin

from .models import Question, QuestionOption, Subject, Tag, Topic, normalize_text, text_hash
from .serializers import (
    BulkActionSerializer, QuestionSerializer, SubjectSerializer, TopicSerializer,
)

MAX_IMAGE_BYTES = 2 * 1024 * 1024


class ProtectedDeleteMixin:
    """Hard delete only when nothing references the record; otherwise explain and suggest archiving."""

    def destroy(self, request, *args, **kwargs):
        obj = self.get_object()
        label = f"{obj.__class__.__name__.lower()} '{obj}'"
        try:
            obj.delete()
        except ProtectedError:
            return Response(
                {"detail": f"This {label} is in use and cannot be deleted. Archive it instead."},
                status=status.HTTP_409_CONFLICT,
            )
        log_activity(request.user, f"{obj.__class__.__name__.lower()}.delete", None, label)
        return Response(status=status.HTTP_204_NO_CONTENT)


class ArchiveMixin:
    @action(detail=True, methods=["post"])
    def archive(self, request, pk=None):
        obj = self.get_object()
        obj.is_active = False
        obj.save(update_fields=["is_active", "updated_at"])
        log_activity(request.user, f"{obj.__class__.__name__.lower()}.archive", obj)
        return Response(self.get_serializer(obj).data)

    @action(detail=True, methods=["post"])
    def restore(self, request, pk=None):
        obj = self.get_object()
        obj.is_active = True
        obj.save(update_fields=["is_active", "updated_at"])
        log_activity(request.user, f"{obj.__class__.__name__.lower()}.restore", obj)
        return Response(self.get_serializer(obj).data)


class SubjectViewSet(ProtectedDeleteMixin, ArchiveMixin, viewsets.ModelViewSet):
    serializer_class = SubjectSerializer
    permission_classes = [IsAdminOrSuperAdmin]
    filterset_fields = ["is_active"]
    search_fields = ["name", "description"]
    ordering_fields = ["name", "created_at"]

    def get_queryset(self):
        return Subject.objects.annotate(
            topic_count=Count("topics", distinct=True), question_count=Count("questions", distinct=True)
        ).order_by("name")

    def perform_create(self, serializer):
        obj = serializer.save()
        log_activity(self.request.user, "subject.create", obj)

    def perform_update(self, serializer):
        obj = serializer.save()
        log_activity(self.request.user, "subject.update", obj)


class TopicViewSet(ProtectedDeleteMixin, ArchiveMixin, viewsets.ModelViewSet):
    serializer_class = TopicSerializer
    permission_classes = [IsAdminOrSuperAdmin]
    filterset_fields = ["subject", "is_active"]
    search_fields = ["name", "subject__name"]
    ordering_fields = ["name", "created_at"]

    def get_queryset(self):
        return Topic.objects.select_related("subject").annotate(question_count=Count("questions")).order_by("subject__name", "name")

    def perform_create(self, serializer):
        obj = serializer.save()
        log_activity(self.request.user, "topic.create", obj)

    def perform_update(self, serializer):
        obj = serializer.save()
        log_activity(self.request.user, "topic.update", obj)


class TagViewSet(viewsets.ReadOnlyModelViewSet):
    queryset = Tag.objects.all()
    permission_classes = [IsAdminOrSuperAdmin]
    search_fields = ["name"]
    pagination_class = None

    def list(self, request, *args, **kwargs):
        qs = self.filter_queryset(self.get_queryset())[:200]
        return Response([t.name for t in qs])


class QuestionFilter(django_filters.FilterSet):
    tag = django_filters.CharFilter(field_name="tags__name", lookup_expr="iexact")

    class Meta:
        model = Question
        fields = ["subject", "topic", "difficulty", "is_active", "source", "question_type"]


class QuestionViewSet(viewsets.ModelViewSet):
    serializer_class = QuestionSerializer
    permission_classes = [IsAdminOrSuperAdmin]
    filterset_class = QuestionFilter
    search_fields = ["text", "text_hi", "explanation"]
    ordering_fields = ["created_at", "updated_at", "difficulty", "marks"]
    parser_classes = [JSONParser, FormParser, MultiPartParser]

    def get_queryset(self):
        return (
            Question.objects.select_related("subject", "topic").prefetch_related("options", "tags").distinct()
        )

    def perform_create(self, serializer):
        obj = serializer.save(created_by=self.request.user)
        log_activity(self.request.user, "question.create", obj)

    def perform_update(self, serializer):
        obj = serializer.save()
        log_activity(self.request.user, "question.update", obj, f"v{obj.version}")

    def destroy(self, request, *args, **kwargs):
        # Questions later become part of tests and attempts, so removal is archiving.
        obj = self.get_object()
        obj.is_active = False
        obj.save(update_fields=["is_active", "updated_at"])
        log_activity(request.user, "question.archive", obj)
        return Response(status=status.HTTP_204_NO_CONTENT)

    @action(detail=True, methods=["post"])
    def duplicate(self, request, pk=None):
        src = self.get_object()
        with transaction.atomic():
            copy = Question.objects.create(
                text=src.text, text_hi=src.text_hi, explanation_hi=src.explanation_hi, subject=src.subject, topic=src.topic, difficulty=src.difficulty,
                question_type=src.question_type, explanation=src.explanation, marks=src.marks,
                negative_marks=src.negative_marks, is_active=False, created_by=request.user,
                source=src.source, source_info=f"Duplicated from #{src.pk}",
            )
            QuestionOption.objects.bulk_create(
                QuestionOption(question=copy, text=o.text, text_hi=o.text_hi, is_correct=o.is_correct, order=o.order)
                for o in src.options.all()
            )
            copy.tags.set(src.tags.all())
        log_activity(request.user, "question.duplicate", copy, f"from #{src.pk}")
        return Response(self.get_serializer(copy).data, status=status.HTTP_201_CREATED)

    @action(detail=True, methods=["post", "delete"], url_path="image")
    def image(self, request, pk=None):
        q = self.get_object()
        if request.method == "DELETE":
            if q.image:
                q.image.delete(save=True)
            return Response(self.get_serializer(q).data)
        f = request.FILES.get("image")
        if not f:
            raise serializers.ValidationError({"image": "No file uploaded."})
        if f.size > MAX_IMAGE_BYTES:
            raise serializers.ValidationError({"image": "Image must be 2 MB or smaller."})
        if not (f.content_type or "").startswith("image/"):
            raise serializers.ValidationError({"image": "File must be an image."})
        try:
            Image.open(f).verify()  # reject files that only claim to be images
        except Exception:
            raise serializers.ValidationError({"image": "Upload a valid image file."})
        f.seek(0)
        if q.image:
            q.image.delete(save=False)
        q.image = f
        q.save()
        return Response(self.get_serializer(q).data)

    @action(detail=False, methods=["post"], url_path="check-duplicates")
    def check_duplicates(self, request):
        text = (request.data.get("text") or "").strip()
        exclude = request.data.get("exclude_id")
        if len(text) < 5:
            return Response([])
        qs = Question.objects.all()
        if exclude:
            qs = qs.exclude(pk=exclude)
        norm = normalize_text(text)
        h = text_hash(text)
        found = []
        subject = request.data.get("subject")
        candidates = qs.filter(subject_id=subject) if subject else qs
        for q in candidates.only("id", "text", "text_hi", "text_hash")[:1000]:
            if q.text_hash == h:
                ratio = 1.0
            else:
                matcher = difflib.SequenceMatcher(None, norm, normalize_text(q.display_text))
                if matcher.quick_ratio() < 0.85:
                    continue
                ratio = matcher.ratio()
                if ratio < 0.85:
                    continue
            found.append({"id": q.id, "text": q.display_text[:200], "similarity": round(ratio, 2), "exact": ratio == 1.0})
        found.sort(key=lambda x: -x["similarity"])
        return Response(found[:5])

    MAX_PICK = 300

    @action(detail=False, methods=["post"])
    def pick(self, request):
        """
        Choose many active questions at once for a test: the newest N or a random N that match the given filters.
        Body: {count, mode: "random"|"newest", subject?, topic?, difficulty?, tag?, search?, exclude_ids?}.
        """
        body = request.data

        def as_int(name, default=None):
            raw = body.get(name, default)
            if raw in (None, ""):
                return None
            try:
                return int(raw)
            except (TypeError, ValueError):
                raise serializers.ValidationError({name: "Must be a number."})

        count = as_int("count")
        if not count or not 1 <= count <= self.MAX_PICK:
            raise serializers.ValidationError({"count": f"Choose between 1 and {self.MAX_PICK} questions."})
        mode = body.get("mode", "newest")
        if mode not in ("random", "newest"):
            raise serializers.ValidationError({"mode": "Use random or newest."})
        exclude = body.get("exclude_ids") or []
        if not isinstance(exclude, list) or not all(isinstance(i, int) for i in exclude):
            raise serializers.ValidationError({"exclude_ids": "Send a list of question ids."})

        qs = Question.objects.filter(is_active=True).exclude(pk__in=exclude)
        if (n := as_int("subject")) is not None:
            qs = qs.filter(subject_id=n)
        if (n := as_int("topic")) is not None:
            qs = qs.filter(topic_id=n)
        if body.get("difficulty") in ("easy", "medium", "hard"):
            qs = qs.filter(difficulty=body["difficulty"])
        if tag := (body.get("tag") or "").strip():
            qs = qs.filter(tags__name__iexact=tag)
        if text := (body.get("search") or "").strip():
            qs = qs.filter(text__icontains=text)
        qs = qs.distinct()

        available = qs.count()
        if mode == "random":
            chosen = random.sample(list(qs.values_list("id", flat=True)), min(count, available))
            by_id = {q.id: q for q in Question.objects.filter(pk__in=chosen).select_related("subject", "topic")}
            rows = [by_id[i] for i in chosen]
        else:
            rows = list(qs.select_related("subject", "topic").order_by("-created_at", "-id")[:count])
        return Response({
            "available": available,
            "questions": [
                {
                    "id": q.id, "text": q.display_text, "subject_name": q.subject.name, "topic_name": q.topic.name if q.topic else None,
                    "difficulty": q.difficulty, "marks": str(q.marks), "negative_marks": str(q.negative_marks), "version": q.version,
                }
                for q in rows
            ],
        })

    @action(detail=False, methods=["post"])
    def bulk(self, request):
        ser = BulkActionSerializer(data=request.data)
        ser.is_valid(raise_exception=True)
        d = ser.validated_data
        qs = Question.objects.filter(pk__in=d["ids"])
        count = qs.count()
        with transaction.atomic():
            if d["action"] == "activate":
                qs.update(is_active=True)
            elif d["action"] == "deactivate":
                qs.update(is_active=False)
            elif d["action"] == "set_difficulty":
                qs.update(difficulty=d["difficulty"])
            else:
                tags = [Tag.objects.get_or_create(name=n.strip().lower())[0] for n in d["tags"] if n.strip()]
                for q in qs:
                    q.tags.add(*tags)
        log_activity(request.user, f"question.bulk_{d['action']}", None, f"{count} questions")
        return Response({"updated": count})
