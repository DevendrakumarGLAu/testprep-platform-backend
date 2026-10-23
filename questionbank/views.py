import datetime
import difflib
import random

import django_filters
from django.db import transaction
from django.db.models import Count, F, ProtectedError
from PIL import Image
from django.shortcuts import get_object_or_404
from django.utils import timezone
from rest_framework import mixins, serializers, status, viewsets
from rest_framework.decorators import action
from rest_framework.parsers import FormParser, JSONParser, MultiPartParser
from rest_framework.response import Response

from accounts.models import log_activity
from notifications import services as notifications
from notifications.models import Notification
from accounts.permissions import IsAdminOrSuperAdmin

from .models import QuestionReport, Question, QuestionOption, Subject, Tag, Topic, normalize_text, text_hash
from .serializers import (
    BulkActionSerializer, QuestionReportSerializer, QuestionSerializer, SubjectSerializer, TopicSerializer,
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
    # Ids to leave out, comma separated: the test builder passes the questions the test already has, so the count and the
    # pages only cover questions that can still be added.
    exclude_ids = django_filters.CharFilter(method="filter_exclude_ids")
    used = django_filters.BooleanFilter(method="filter_used")
    reported = django_filters.BooleanFilter(method="filter_reported")
    date_from = django_filters.DateFilter(field_name="question_date", lookup_expr="gte")
    date_to = django_filters.DateFilter(field_name="question_date", lookup_expr="lte")

    def filter_reported(self, queryset, name, value):
        """reported=true: has an open student report."""
        open_ids = QuestionReport.objects.filter(status="open").values("question_id")
        return queryset.filter(pk__in=open_ids) if value else queryset.exclude(pk__in=open_ids)

    def filter_used(self, queryset, name, value):
        """used=true: already in a (not archived) test; used=false: in none yet."""
        in_a_test = Question.objects.filter(exam_items__section__exam__status__in=("draft", "published")).values("pk")
        return queryset.filter(pk__in=in_a_test) if value else queryset.exclude(pk__in=in_a_test)

    def filter_exclude_ids(self, queryset, name, value):
        ids = [int(x) for x in str(value).split(",") if x.strip().isdigit()]
        return queryset.exclude(pk__in=ids) if ids else queryset

    class Meta:
        model = Question
        fields = ["subject", "topic", "difficulty", "is_active", "source", "question_type", "review_status"]


class QuestionViewSet(viewsets.ModelViewSet):
    serializer_class = QuestionSerializer
    permission_classes = [IsAdminOrSuperAdmin]
    filterset_class = QuestionFilter
    search_fields = ["text", "text_hi", "explanation"]
    ordering_fields = ["created_at", "updated_at", "difficulty", "marks", "question_date", "id"]
    parser_classes = [JSONParser, FormParser, MultiPartParser]

    def get_queryset(self):
        return (
            Question.objects.select_related("subject", "topic").prefetch_related("options", "tags", "exam_items__section__exam", "reports").distinct()
        )

    def filter_queryset(self, queryset):
        """Always end the sort with the id: many questions share a date, and a tie leaves page boundaries undefined."""
        qs = super().filter_queryset(queryset)
        order = list(qs.query.order_by) or list(Question._meta.ordering)
        if not any(str(o).lstrip("-") in ("id", "pk") for o in order):
            qs = qs.order_by(*order, "-id")
        return qs

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
                text=src.text, text_hi=src.text_hi, explanation_hi=src.explanation_hi, video_url=src.video_url, question_date=src.question_date, subject=src.subject, topic=src.topic, difficulty=src.difficulty,
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
        if str(body.get("used", "")).lower() in ("true", "false"):
            in_a_test = Question.objects.filter(exam_items__section__exam__status__in=("draft", "published")).values("pk")
            qs = qs.filter(pk__in=in_a_test) if str(body["used"]).lower() == "true" else qs.exclude(pk__in=in_a_test)
        for key, lookup in (("date_from", "question_date__gte"), ("date_to", "question_date__lte")):
            if raw := body.get(key):
                try:
                    qs = qs.filter(**{lookup: datetime.date.fromisoformat(str(raw))})
                except ValueError:
                    raise serializers.ValidationError({key: "Use the format YYYY-MM-DD."})
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
            rows = list(qs.select_related("subject", "topic").order_by(F("question_date").desc(nulls_last=True), "-created_at", "-id")[:count])
        return Response({
            "available": available,
            "questions": [
                {
                    "id": q.id, "text": q.display_text, "subject_name": q.subject.name, "topic_name": q.topic.name if q.topic else None,
                    "difficulty": q.difficulty, "question_date": q.question_date, "marks": str(q.marks), "negative_marks": str(q.negative_marks), "version": q.version,
                }
                for q in rows
            ],
        })

    MAX_BULK = 500

    @action(detail=False, methods=["get"])
    def ids(self, request):
        """The ids of every question matching the list's filters (up to 500), so "select all matching" can act on all pages."""
        qs = self.filter_queryset(self.get_queryset())
        total = qs.count()
        ids = list(qs.values_list("id", flat=True)[: self.MAX_BULK])
        return Response({"count": total, "ids": ids, "truncated": total > len(ids)})

    @action(detail=False, methods=["get"])
    def duplicates(self, request):
        """Groups of active questions with exactly the same text, newest group first, so repeats can be cleaned up."""
        hashes = list(
            Question.objects.filter(is_active=True).exclude(text_hash="")
            .values("text_hash").annotate(n=Count("id")).filter(n__gt=1).order_by("-n", "text_hash")[:50]
        )
        qs = (
            Question.objects.filter(is_active=True, text_hash__in=[h["text_hash"] for h in hashes])
            .select_related("subject").prefetch_related("exam_items__section__exam").order_by("id")
        )
        groups: dict[str, list] = {}
        for q in qs:
            groups.setdefault(q.text_hash, []).append(q)
        out = []
        for h in hashes:
            members = groups.get(h["text_hash"], [])
            out.append({
                "text": members[0].display_text[:200] if members else "",
                "questions": [
                    {
                        "id": q.id, "subject": q.subject.name, "question_date": q.question_date, "review_status": q.review_status,
                        "created_at": q.created_at,
                        "used_in": sorted({i.section.exam.title for i in q.exam_items.all() if i.section.exam.status != "archived"}),
                    }
                    for q in members
                ],
            })
        return Response({"groups": out})

    @action(detail=False, methods=["post"], url_path="duplicates/resolve")
    def resolve_duplicates(self, request):
        """{keep, archive: [ids]}: archive the other copies of one question. Only true copies of `keep` can be archived."""
        keep = request.data.get("keep")
        archive = request.data.get("archive")
        if not isinstance(keep, int) or not isinstance(archive, list) or not archive or not all(isinstance(i, int) for i in archive):
            raise serializers.ValidationError({"detail": "Send the question to keep and the copies to archive."})
        kept = get_object_or_404(Question, pk=keep)
        copies = Question.objects.filter(pk__in=archive, text_hash=kept.text_hash).exclude(pk=kept.pk)
        if copies.count() != len(set(archive)):
            raise serializers.ValidationError({"archive": "Only exact copies of the question you keep can be archived."})
        used = sorted({i.section.exam.title for q in copies.prefetch_related("exam_items__section__exam")
                       for i in q.exam_items.all() if i.section.exam.status != "archived"})
        with transaction.atomic():
            n = copies.update(is_active=False)
        log_activity(request.user, "question.archive_duplicates", kept, f"{n} copies archived")
        return Response({"archived": n, "still_used_in": used})

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
            elif d["action"] == "set_review_status":
                qs.update(review_status=d["review_status"])
            elif d["action"] == "set_question_date":
                qs.update(question_date=d["question_date"])
            elif d["action"] == "set_subject":
                qs.update(subject=d["subject"], topic=None)  # topics belong to a subject, so the old one no longer applies
            else:
                tags = [Tag.objects.get_or_create(name=n.strip().lower())[0] for n in d["tags"] if n.strip()]
                for q in qs:
                    q.tags.add(*tags)
        log_activity(request.user, f"question.bulk_{d['action']}", None, f"{count} questions")
        return Response({"updated": count})


class QuestionReportViewSet(mixins.ListModelMixin, mixins.RetrieveModelMixin, mixins.UpdateModelMixin, viewsets.GenericViewSet):
    """The queue of problems students reported. An admin marks each one fixed or rejected, with a note the student can read."""

    queryset = QuestionReport.objects.select_related("question", "student")
    serializer_class = QuestionReportSerializer
    permission_classes = [IsAdminOrSuperAdmin]
    filterset_fields = ["status", "reason", "question"]
    search_fields = ["message", "question__text", "question__text_hi", "exam_title"]
    ordering_fields = ["created_at", "status"]
    http_method_names = ["get", "patch", "head", "options"]

    def perform_update(self, serializer):
        report = serializer.save(resolved_by=self.request.user, resolved_at=timezone.now())
        if report.status == QuestionReport.Status.OPEN:  # reopened
            report.resolved_by, report.resolved_at = None, None
            report.save(update_fields=["resolved_by", "resolved_at"])
        # The question is cleared for use again once no report on it is open.
        q = report.question
        if not q.reports.filter(status="open").exists() and q.review_status != "approved":
            q.review_status = "approved"
            q.save(update_fields=["review_status", "updated_at"])
        elif q.reports.filter(status="open").exists() and q.review_status != "needs_review":
            q.review_status = "needs_review"
            q.save(update_fields=["review_status", "updated_at"])
        log_activity(self.request.user, "question.report_" + report.status, q, f"report #{report.id}")
        if report.status != QuestionReport.Status.OPEN:
            verdict = "was fixed" if report.status == QuestionReport.Status.RESOLVED else "needs no change"
            notifications.notify(
                [report.student_id], Notification.Kind.REPORT, f"Your report {verdict}", report.admin_note or q.display_text[:120],
                "/student/saved", ref=f"report:{report.id}:{report.status}",
            )
