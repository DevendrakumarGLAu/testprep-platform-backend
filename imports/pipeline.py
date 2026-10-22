"""
The one pipeline every source goes through: raw rows -> normalised questions -> validation ->
duplicate detection -> (after review) persistence. CSV, Excel and PDF all end up as a table of
raw rows, so a future website extractor only has to produce the same table.
"""
import difflib
import re
from decimal import Decimal, InvalidOperation

from django.db import transaction
from django.utils import timezone

from questionbank.models import Question, Subject, Topic, normalize_text, text_hash
from questionbank.serializers import QuestionSerializer

from .models import ImportJob, ImportRow

MAX_OPTIONS = 10
LETTERS = "ABCDEFGHIJ"

# Canonical field -> header names we recognise automatically (compared lower-case, punctuation stripped).
SYNONYMS = {
    "question": ["question", "question text", "q", "text", "stem", "question statement"],
    "correct": ["correct answer", "correct", "answer", "ans", "right answer", "correct option", "key"],
    "explanation": ["explanation", "solution", "rationale", "explain", "answer explanation"],
    "subject": ["subject"],
    "topic": ["topic", "chapter"],
    "difficulty": ["difficulty", "level", "difficulty level"],
    "marks": ["marks", "positive marks", "score", "points", "mark"],
    "negative_marks": ["negative marks", "negative", "penalty", "negative marking", "negative mark"],
    "tags": ["tags", "tag", "labels", "keywords"],
}
OPTION_HEADER = re.compile(r"(?:option|opt|choice)?\s*([a-j]|10|[1-9])")

TEMPLATE_HEADERS = [
    "question", "option_a", "option_b", "option_c", "option_d", "option_e", "option_f",
    "correct_answer", "explanation", "subject", "topic", "difficulty", "marks", "negative_marks", "tags",
]
TEMPLATE_EXAMPLE = [
    "What is the capital of France?", "Paris", "Rome", "Berlin", "Madrid", "", "",
    "A", "Paris has been the capital since 987.", "Geography", "Capitals", "easy", "1", "0.25", "europe;capitals",
]


def _key(h: str) -> str:
    return re.sub(r"[^a-z0-9 ]", " ", h.lower()).strip()


def _option_rank(header: str):
    """'Option A', 'option_a', 'A', 'Choice 3' -> 0-based position; None if it is not an option column."""
    m = OPTION_HEADER.fullmatch(header.strip().lower().replace("_", " ").replace("-", " "))
    if not m:
        return None
    t = m.group(1)
    return LETTERS.index(t.upper()) if t.isalpha() else int(t) - 1


def guess_mapping(headers: list[str]) -> dict:
    """Best-effort header matching; the user can change every choice in the wizard."""
    mapping: dict = {"options": []}
    taken = set()
    for field, names in SYNONYMS.items():
        for h in headers:
            if h not in taken and _key(h) in names:
                mapping[field] = h
                taken.add(h)
                break
    opts = []
    for h in headers:
        if h in taken:
            continue
        rank = _option_rank(h)
        if rank is not None and _key(h) not in sum(SYNONYMS.values(), []):
            opts.append((rank, h))
    mapping["options"] = [h for _, h in sorted(opts)][:MAX_OPTIONS]
    return mapping


# --- normalisation / validation --------------------------------------------------------------

def _dec(value, default, field, errors, allow_negative_sign=False):
    value = (value or "").strip()
    if not value:
        return default
    try:
        d = Decimal(value.replace(",", "."))
    except InvalidOperation:
        errors.append(f"{field} '{value}' is not a number.")
        return default
    if allow_negative_sign:
        d = abs(d)  # "-0.25" means the same deduction as "0.25"
    return d


def normalize_row(raw: dict, mapping: dict, defaults: dict, ctx: "Lookup", create_missing: bool):
    errors: list[str] = []
    cell = lambda h: str(raw.get(h, "") if h else "").strip()  # noqa: E731

    text = cell(mapping.get("question"))
    if not text:
        errors.append("Question text is missing.")

    options = []
    for i, h in enumerate(mapping.get("options", [])[:MAX_OPTIONS]):
        t = cell(h)
        if t:
            options.append({"label": LETTERS[i], "text": t})
    if len(options) < 2:
        errors.append("At least two options are required.")
    if len({o["text"].lower() for o in options}) != len(options):
        errors.append("Option texts must be unique.")

    correct_raw = cell(mapping.get("correct"))
    correct_label = None
    if not correct_raw:
        errors.append("Correct answer is missing.")
    elif options:
        labels = [o["label"] for o in options]
        up = correct_raw.upper().strip().rstrip(".)")
        if re.fullmatch(r"[A-J]", up) and up in labels:
            correct_label = up
        elif re.fullmatch(r"\d{1,2}", up) and 1 <= int(up) <= MAX_OPTIONS and LETTERS[int(up) - 1] in labels:
            correct_label = LETTERS[int(up) - 1]
        else:
            hits = [o["label"] for o in options if o["text"].lower() == correct_raw.lower()]
            if len(hits) == 1:
                correct_label = hits[0]
        if correct_label is None:
            if re.search(r"[,;/&]|\band\b", correct_raw, re.I) and len(correct_raw) < 20:
                errors.append("Only single-correct questions are supported; several correct answers were given.")
            else:
                errors.append(f"Correct answer '{correct_raw}' does not match any option.")

    # subject / topic
    subject_name = cell(mapping.get("subject")) or defaults.get("subject_name", "")
    subject = ctx.subject(subject_name)
    new_subject = False
    if not subject_name:
        errors.append("Subject is missing. Add a subject column or choose a default subject.")
    elif subject is None:
        if create_missing:
            new_subject = True
        else:
            errors.append(f"Unknown subject '{subject_name}'.")

    topic_name = cell(mapping.get("topic")) or defaults.get("topic_name", "")
    new_topic = False
    topic = None
    if topic_name:
        if subject is not None:
            topic = ctx.topic(subject, topic_name)
            if topic is None:
                if create_missing:
                    new_topic = True
                else:
                    errors.append(f"Unknown topic '{topic_name}' in subject '{subject.name}'.")
        elif new_subject:
            new_topic = True

    diff_raw = cell(mapping.get("difficulty")).lower() or defaults.get("difficulty", "medium")
    diff = {"e": "easy", "easy": "easy", "m": "medium", "medium": "medium", "med": "medium", "h": "hard", "hard": "hard"}.get(diff_raw)
    if diff is None:
        errors.append(f"Difficulty '{diff_raw}' must be easy, medium or hard.")
        diff = "medium"

    marks = _dec(cell(mapping.get("marks")), Decimal(str(defaults.get("marks", 1))), "Marks", errors)
    if marks <= 0:
        errors.append("Marks must be greater than zero.")
    neg = _dec(cell(mapping.get("negative_marks")), Decimal(str(defaults.get("negative_marks", 0))), "Negative marks", errors, True)

    tags = [t.strip().lower() for t in re.split(r"[,;|]", cell(mapping.get("tags"))) if t.strip()]

    normalized = {
        "question": text, "options": options, "correct_label": correct_label,
        "explanation": cell(mapping.get("explanation")), "subject": subject_name, "topic": topic_name,
        "subject_id": subject.id if subject else None, "topic_id": topic.id if topic else None,
        "new_subject": new_subject, "new_topic": new_topic, "difficulty": diff,
        "marks": str(marks), "negative_marks": str(neg), "tags": tags[:15],
    }
    return normalized, errors


class Lookup:
    """Caches subjects, topics and existing question texts for one processing run."""

    def __init__(self):
        self.subjects = {s.name.lower(): s for s in Subject.objects.filter(is_active=True)}
        self.topics = {(t.subject_id, t.name.lower()): t for t in Topic.objects.filter(is_active=True)}
        self.by_hash: dict[str, int] = {}
        self.by_subject: dict[int, list[tuple[int, str]]] = {}
        for qid, h, sid, text in Question.objects.values_list("id", "text_hash", "subject_id", "text")[:20000]:
            self.by_hash.setdefault(h, qid)
            self.by_subject.setdefault(sid, []).append((qid, normalize_text(text)))

    def subject(self, name):
        return self.subjects.get((name or "").lower())

    def topic(self, subject, name):
        return self.topics.get((subject.id, (name or "").lower())) if subject else None

    def find_duplicate(self, text, subject_id):
        """Returns (question_id, similarity, exact) or None."""
        h = text_hash(text)
        if h in self.by_hash:
            return self.by_hash[h], 1.0, True
        norm = normalize_text(text)
        pool = self.by_subject.get(subject_id, []) if subject_id else [p for lst in self.by_subject.values() for p in lst]
        best = None
        for qid, other in pool:
            if not other or abs(len(other) - len(norm)) > max(len(norm), len(other)) * 0.3:
                continue
            m = difflib.SequenceMatcher(None, norm, other)
            if m.quick_ratio() < 0.9:
                continue
            r = m.ratio()
            if r >= 0.9 and (best is None or r > best[1]):
                best = (qid, r, False)
        return best


@transaction.atomic
def process_job(job: ImportJob, mapping: dict, defaults: dict, create_missing: bool) -> ImportJob:
    """(Re)runs normalisation, validation and duplicate detection over every stored row."""
    if job.status == ImportJob.Status.IMPORTED:
        raise ValueError("This import has already been completed.")
    ctx = Lookup()
    if defaults.get("subject"):
        s = Subject.objects.filter(pk=defaults["subject"]).first()
        defaults["subject_name"] = s.name if s else ""
    if defaults.get("topic"):
        t = Topic.objects.filter(pk=defaults["topic"]).first()
        defaults["topic_name"] = t.name if t else ""

    seen_in_file: dict[str, int] = {}
    valid = invalid = dup = 0
    rows = list(job.rows.all())
    for row in rows:
        normalized, errors = normalize_row(row.raw, mapping, defaults, ctx, create_missing)
        row.normalized, row.errors = normalized, errors
        row.duplicate_of, row.duplicate_info, row.selected = None, None, False
        if errors:
            row.status = ImportRow.State.INVALID
            invalid += 1
        else:
            h = text_hash(normalized["question"])
            found = ctx.find_duplicate(normalized["question"], normalized["subject_id"])
            if h in seen_in_file:
                row.status = ImportRow.State.DUPLICATE
                row.duplicate_info = {"in_file": True, "row": seen_in_file[h], "similarity": 1.0, "exact": True}
                dup += 1
            elif found:
                row.status = ImportRow.State.DUPLICATE
                row.duplicate_of_id = found[0]
                row.duplicate_info = {"in_file": False, "question": found[0], "similarity": round(found[1], 2), "exact": found[2]}
                dup += 1
            else:
                row.status = ImportRow.State.VALID
                row.selected = True
                valid += 1
            seen_in_file.setdefault(h, row.row_number)
    ImportRow.objects.bulk_update(rows, ["normalized", "errors", "duplicate_of", "duplicate_info", "selected", "status"])

    job.mapping, job.defaults, job.create_missing = mapping, defaults, create_missing
    job.total_rows, job.valid_rows, job.invalid_rows, job.duplicate_rows = len(rows), valid, invalid, dup
    job.status = ImportJob.Status.PROCESSED
    job.save()
    return job


def recount(job: ImportJob):
    job.valid_rows = job.rows.filter(status=ImportRow.State.VALID).count()
    job.invalid_rows = job.rows.filter(status=ImportRow.State.INVALID).count()
    job.duplicate_rows = job.rows.filter(status=ImportRow.State.DUPLICATE).count()
    job.save(update_fields=["valid_rows", "invalid_rows", "duplicate_rows"])


# --- persistence -----------------------------------------------------------------------------

def commit_job(job: ImportJob, user) -> ImportJob:
    """Imports the selected rows. Each row is its own savepoint, so one bad row never blocks the rest."""
    if job.status != ImportJob.Status.PROCESSED:
        raise ValueError("Process the file and review the rows before importing.")

    imported = failed = skipped = 0
    for row in job.rows.select_related("job"):
        if row.status == ImportRow.State.INVALID or not row.selected:
            row.result, row.result_message = ImportRow.Result.SKIPPED, "Not selected." if row.status != ImportRow.State.INVALID else "Invalid row."
            row.save(update_fields=["result", "result_message"])
            skipped += 1
            continue
        n = row.normalized
        try:
            with transaction.atomic():
                subject = Subject.objects.filter(pk=n["subject_id"]).first() if n["subject_id"] else None
                if subject is None and n["new_subject"]:
                    subject, _ = Subject.objects.get_or_create(name__iexact=n["subject"], defaults={"name": n["subject"]})
                topic = Topic.objects.filter(pk=n["topic_id"]).first() if n["topic_id"] else None
                if topic is None and n["new_topic"] and n["topic"]:
                    topic, _ = Topic.objects.get_or_create(subject=subject, name__iexact=n["topic"], defaults={"name": n["topic"]})
                data = {
                    "text": n["question"], "subject": subject.id, "topic": topic.id if topic else None,
                    "difficulty": n["difficulty"], "marks": n["marks"], "negative_marks": n["negative_marks"],
                    "explanation": n["explanation"], "tags": n["tags"], "allow_duplicate": True,
                    "options": [{"text": o["text"], "is_correct": o["label"] == n["correct_label"]} for o in n["options"]],
                }
                ser = QuestionSerializer(data=data)
                ser.is_valid(raise_exception=True)
                q = ser.save(created_by=user, source=job.source_type, source_info=f"{job.filename} row {row.row_number}")
            row.result, row.question, row.result_message = ImportRow.Result.IMPORTED, q, ""
            imported += 1
        except Exception as e:  # noqa: BLE001 - recorded per row and reported
            detail = getattr(e, "detail", None)
            row.result, row.result_message = ImportRow.Result.FAILED, (str(detail) if detail else str(e))[:300]
            failed += 1
        row.save(update_fields=["result", "result_message", "question"])

    job.imported_count, job.failed_count, job.skipped_count = imported, failed, skipped
    job.status, job.completed_at = ImportJob.Status.IMPORTED, timezone.now()
    job.save()
    return job
