from django.db import transaction
from django.utils import timezone
from rest_framework import serializers

from .models import QuestionReport, Question, QuestionOption, Subject, Tag, Topic, text_hash

MIN_OPTIONS, MAX_OPTIONS = 2, 10


class SubjectSerializer(serializers.ModelSerializer):
    topic_count = serializers.IntegerField(read_only=True)
    question_count = serializers.IntegerField(read_only=True)

    class Meta:
        model = Subject
        fields = ["id", "name", "description", "is_active", "topic_count", "question_count", "created_at"]
        read_only_fields = ["id", "created_at"]

    def validate_name(self, value):
        value = value.strip()
        qs = Subject.objects.filter(name__iexact=value)
        if self.instance:
            qs = qs.exclude(pk=self.instance.pk)
        if qs.exists():
            raise serializers.ValidationError("A subject with this name already exists.")
        return value


class TopicSerializer(serializers.ModelSerializer):
    subject_name = serializers.CharField(source="subject.name", read_only=True)
    question_count = serializers.IntegerField(read_only=True)

    class Meta:
        model = Topic
        fields = ["id", "subject", "subject_name", "name", "description", "is_active", "question_count", "created_at"]
        read_only_fields = ["id", "created_at"]

    def validate(self, attrs):
        name = attrs.get("name", getattr(self.instance, "name", "")).strip()
        subject = attrs.get("subject", getattr(self.instance, "subject", None))
        attrs["name"] = name
        qs = Topic.objects.filter(subject=subject, name__iexact=name)
        if self.instance:
            qs = qs.exclude(pk=self.instance.pk)
        if qs.exists():
            raise serializers.ValidationError({"name": "This subject already has a topic with this name."})
        return attrs


class OptionSerializer(serializers.ModelSerializer):
    id = serializers.IntegerField(required=False)
    text = serializers.CharField(allow_blank=True, required=False, trim_whitespace=True)
    text_hi = serializers.CharField(allow_blank=True, required=False, trim_whitespace=True)

    class Meta:
        model = QuestionOption
        fields = ["id", "text", "text_hi", "is_correct"]


class QuestionSerializer(serializers.ModelSerializer):
    options = OptionSerializer(many=True)
    tags = serializers.ListField(child=serializers.CharField(max_length=40), required=False, write_only=True)
    allow_duplicate = serializers.BooleanField(write_only=True, required=False, default=False)
    subject_name = serializers.CharField(source="subject.name", read_only=True)
    topic_name = serializers.CharField(source="topic.name", read_only=True, default=None)
    image_url = serializers.SerializerMethodField()
    languages = serializers.SerializerMethodField()
    used_in = serializers.SerializerMethodField()
    open_reports = serializers.SerializerMethodField()
    text = serializers.CharField(allow_blank=True, required=False, trim_whitespace=True)
    text_hi = serializers.CharField(allow_blank=True, required=False, trim_whitespace=True)

    class Meta:
        model = Question
        fields = [
            "id", "text", "text_hi", "languages", "explanation_hi", "video_url", "question_date", "used_in", "review_status", "open_reports", "subject", "subject_name", "topic", "topic_name", "difficulty", "question_type",
            "explanation", "marks", "negative_marks", "tags", "options", "image_url", "is_active",
            "source", "source_info", "version", "allow_duplicate", "created_at", "updated_at",
        ]
        read_only_fields = ["id", "source", "source_info", "version", "created_at", "updated_at"]

    def get_image_url(self, obj):
        if not obj.image:
            return None
        request = self.context.get("request")
        return request.build_absolute_uri(obj.image.url) if request else obj.image.url

    def to_representation(self, instance):
        data = super().to_representation(instance)
        data["tags"] = sorted(t.name for t in instance.tags.all())
        return data

    # --- validation -------------------------------------------------------------------------

    def get_open_reports(self, obj):
        return sum(1 for r in obj.reports.all() if r.status == "open")

    def get_used_in(self, obj):
        """The tests (not archived ones) this question is part of, so an admin can see where it is already used."""
        seen = {}
        for item in obj.exam_items.all():
            exam = item.section.exam
            if exam.status != "archived":
                seen[exam.id] = {"id": exam.id, "title": exam.title, "status": exam.status}
        return sorted(seen.values(), key=lambda e: e["title"])

    def get_languages(self, obj):
        return [code for code, ok in (("en", obj.text and all(o.text for o in obj.options.all())),
                                      ("hi", obj.text_hi and all(o.text_hi for o in obj.options.all()))) if ok]

    def validate_question_date(self, value):
        if value and value > timezone.localdate():
            raise serializers.ValidationError("The question date cannot be in the future.")
        return value

    def validate_marks(self, value):
        if value <= 0:
            raise serializers.ValidationError("Marks must be greater than zero.")
        return value

    def validate_negative_marks(self, value):
        if value < 0:
            raise serializers.ValidationError("Negative marks cannot be below zero (enter the deduction as a positive number).")
        return value

    def validate_options(self, options):
        if not MIN_OPTIONS <= len(options) <= MAX_OPTIONS:
            raise serializers.ValidationError(f"Provide between {MIN_OPTIONS} and {MAX_OPTIONS} options.")
        for field in ("text", "text_hi"):
            texts = [o.get(field, "").strip().lower() for o in options if o.get(field, "").strip()]
            if len(set(texts)) != len(texts):
                raise serializers.ValidationError("Option texts must be unique.")
        if sum(1 for o in options if o.get("is_correct")) != 1:
            raise serializers.ValidationError("Exactly one option must be marked correct.")
        if self.instance:
            owned = set(self.instance.options.values_list("id", flat=True))
            for o in options:
                if "id" in o and o["id"] not in owned:
                    raise serializers.ValidationError("An option does not belong to this question.")
        return options

    def validate(self, attrs):
        subject = attrs.get("subject", getattr(self.instance, "subject", None))
        topic = attrs.get("topic", getattr(self.instance, "topic", None))
        if topic and topic.subject_id != subject.id:
            raise serializers.ValidationError({"topic": "This topic does not belong to the selected subject."})
        if "subject" in attrs and not attrs["subject"].is_active and (
            not self.instance or attrs["subject"].id != self.instance.subject_id
        ):
            raise serializers.ValidationError({"subject": "This subject is archived."})
        if topic and not topic.is_active and (not self.instance or topic.id != self.instance.topic_id):
            raise serializers.ValidationError({"topic": "This topic is archived."})

        # Language rule: English or Hindi, at least one, and a language that is started must be finished.
        text = attrs.get("text", getattr(self.instance, "text", "")) or ""
        text_hi = attrs.get("text_hi", getattr(self.instance, "text_hi", "")) or ""
        opts = attrs.get("options")
        if opts is None and self.instance:
            opts = [{"text": o.text, "text_hi": o.text_hi} for o in self.instance.options.all()]
        opts = opts or []
        complete = []
        for code, label, stem, field in (("en", "English", text, "text"), ("hi", "Hindi", text_hi, "text_hi")):
            filled = [bool((o.get(field) or "").strip()) for o in opts]
            if stem.strip() and all(filled):
                complete.append(code)
            elif stem.strip() or any(filled):
                raise serializers.ValidationError(
                    {field: f"The {label} version is incomplete: it needs the question and every option (or leave {label} empty)."}
                )
        if not complete:
            raise serializers.ValidationError({"text": "Enter the question and its options in English, Hindi, or both."})

        text = text.strip() or text_hi.strip()
        if not attrs.get("allow_duplicate") and ("text" in attrs or "text_hi" in attrs):
            qs = Question.objects.filter(text_hash=text_hash(text))
            if self.instance:
                qs = qs.exclude(pk=self.instance.pk)
            dup = qs.first()
            if dup:
                raise serializers.ValidationError(
                    {"text": f"A question with the same text already exists (#{dup.pk}). Tick 'save anyway' to keep both."}
                )
        return attrs

    # --- persistence ------------------------------------------------------------------------

    def _set_tags(self, question, names):
        tags = []
        for name in {n.strip().lower() for n in names if n.strip()}:
            tag, _ = Tag.objects.get_or_create(name=name)
            tags.append(tag)
        question.tags.set(tags)

    @transaction.atomic
    def create(self, validated_data):
        options = validated_data.pop("options")
        tags = validated_data.pop("tags", [])
        validated_data.pop("allow_duplicate", None)
        validated_data.setdefault("question_date", timezone.localdate())  # typed in today unless the admin says otherwise
        question = Question.objects.create(**validated_data)
        for i, o in enumerate(options):
            QuestionOption.objects.create(
                question=question, text=o.get("text", ""), text_hi=o.get("text_hi", ""), is_correct=o["is_correct"], order=i
            )
        self._set_tags(question, tags)
        return question

    @transaction.atomic
    def update(self, instance, validated_data):
        options = validated_data.pop("options", None)
        tags = validated_data.pop("tags", None)
        validated_data.pop("allow_duplicate", None)

        scoring_changed = any(
            k in validated_data and validated_data[k] != getattr(instance, k)
            for k in ("text", "text_hi", "marks", "negative_marks")
        )
        for k, v in validated_data.items():
            setattr(instance, k, v)

        if options is not None:
            # Update in place so option ids stay stable for historical attempts.
            existing = {o.id: o for o in instance.options.all()}
            keep = set()
            for i, o in enumerate(options):
                opt = existing.get(o.get("id"))
                if opt:
                    new = (o.get("text", opt.text), o.get("text_hi", opt.text_hi), o["is_correct"], i)
                    if (opt.text, opt.text_hi, opt.is_correct, opt.order) != new:
                        scoring_changed = True
                    opt.text, opt.text_hi, opt.is_correct, opt.order = new
                    opt.save()
                    keep.add(opt.id)
                else:
                    scoring_changed = True
                    keep.add(
                        QuestionOption.objects.create(
                            question=instance, text=o.get("text", ""), text_hi=o.get("text_hi", ""), is_correct=o["is_correct"], order=i
                        ).id
                    )
            removed = instance.options.exclude(id__in=keep)
            if removed.exists():
                scoring_changed = True
                removed.delete()

        if scoring_changed:
            instance.version += 1
        instance.save()
        if tags is not None:
            self._set_tags(instance, tags)
        return instance


class BulkActionSerializer(serializers.Serializer):
    ids = serializers.ListField(child=serializers.IntegerField(), allow_empty=False, max_length=500)
    action = serializers.ChoiceField(choices=["activate", "deactivate", "set_difficulty", "add_tags", "set_review_status", "set_question_date", "set_subject"])
    difficulty = serializers.ChoiceField(choices=["easy", "medium", "hard"], required=False)
    review_status = serializers.ChoiceField(choices=["approved", "needs_review"], required=False)
    question_date = serializers.DateField(required=False)
    subject = serializers.PrimaryKeyRelatedField(queryset=Subject.objects.filter(is_active=True), required=False)
    tags = serializers.ListField(child=serializers.CharField(max_length=40), required=False)

    def validate(self, attrs):
        if attrs["action"] == "set_difficulty" and "difficulty" not in attrs:
            raise serializers.ValidationError({"difficulty": "Required for this action."})
        if attrs["action"] == "set_question_date":
            if "question_date" not in attrs:
                raise serializers.ValidationError({"question_date": "Required for this action."})
            if attrs["question_date"] > timezone.localdate():
                raise serializers.ValidationError({"question_date": "The question date cannot be in the future."})
        if attrs["action"] == "set_subject" and "subject" not in attrs:
            raise serializers.ValidationError({"subject": "Required for this action."})
        if attrs["action"] == "set_review_status" and "review_status" not in attrs:
            raise serializers.ValidationError({"review_status": "Required for this action."})
        if attrs["action"] == "add_tags" and not attrs.get("tags"):
            raise serializers.ValidationError({"tags": "Required for this action."})
        return attrs



class QuestionReportSerializer(serializers.ModelSerializer):
    """What an admin sees in the reports queue."""

    question_text = serializers.SerializerMethodField()
    student_name = serializers.SerializerMethodField()
    reason_label = serializers.CharField(source="get_reason_display", read_only=True)

    class Meta:
        model = QuestionReport
        fields = [
            "id", "question", "question_text", "student_name", "exam_title", "reason", "reason_label", "message", "status",
            "admin_note", "resolved_at", "created_at",
        ]
        read_only_fields = ["id", "question", "question_text", "student_name", "exam_title", "reason", "reason_label", "message",
                            "resolved_at", "created_at"]

    def get_question_text(self, obj):
        return obj.question.display_text[:200]

    def get_student_name(self, obj):
        s = obj.student
        return (f"{s.first_name} {s.last_name}").strip() or s.username

    def validate_status(self, value):
        return value
