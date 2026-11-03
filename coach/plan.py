"""Study plans: turning an exam date into a day-by-day list of tests and revision, and keeping the list honest."""
from datetime import timedelta
from urllib.parse import quote

from django.db.models import Q
from django.utils import timezone

from attempts import services as svc
from attempts.models import Attempt
from attempts.practice import MIN_ANSWERED_FOR_WEAK, WEAK_BELOW_PERCENT, topic_stats
from exams.models import Exam
from questionbank.models import Question, Subject

from .models import StudyPlan, StudyTask

MAX_DAYS = 180
MIN_MINUTES, MAX_MINUTES = 30, 240
UNKNOWN_ACCURACY_WEIGHT = 1.5  # a subject the student has not answered yet is looked at a little more than an average one


def candidate_exams(student, category=None):
    """The tests a student can be given: their own exam's tests first when they chose one, never-attempted ones before the rest."""
    now = timezone.now()
    # Not live tests, and not tests whose window is over or has not opened yet (the student could not start them on the day).
    qs = svc.visible_exams(student).filter(is_live=False).filter(Q(end_at__isnull=True) | Q(end_at__gte=now)).filter(Q(start_at__isnull=True) | Q(start_at__lte=now))
    if category is not None:
        qs = qs.filter(Q(category=category) | Q(also_in=category)).distinct()
    exams = [e for e in qs.order_by("id") if not e.max_attempts or svc.completed_count(e, student) < e.max_attempts]
    done = set(Attempt.objects.filter(student=student, status=Attempt.Status.SUBMITTED, exam__in=exams).values_list("exam_id", flat=True))
    return [e for e in exams if e.id not in done] + [e for e in exams if e.id in done]


# --- subjects -------------------------------------------------------------------------------

def subject_accuracy(student) -> dict:
    """Per subject id: how many answers the student has given, how many were right, and the accuracy (None when there are none)."""
    out: dict[int, dict] = {}
    for t in topic_stats(student):
        if t["subject_id"] is None:
            continue
        row = out.setdefault(t["subject_id"], {"answered": 0, "correct": 0})
        row["answered"] += t["answered"]
        row["correct"] += t["correct"]
    for row in out.values():
        row["accuracy"] = round(row["correct"] * 100 / row["answered"], 1) if row["answered"] else None
    return out


def plan_subjects(plan: StudyPlan, student, pool) -> list[Subject]:
    """
    The subjects this plan covers: the sections of the chosen exam, else the subjects of the subject tests on offer, else the subjects
    the student has already answered questions in. A subject needs something to study (a subject test, or questions to practise).
    """
    found: dict[int, Subject] = {}
    if plan.category_id:
        found = {s.id: s for s in plan.category.subjects.filter(is_active=True)}
    if not found:
        found = {e.subject_id: e.subject for e in pool if e.test_type == Exam.TestType.SECTIONAL and e.subject_id and e.subject.is_active}
    if not found:
        ids = list(subject_accuracy(student))
        found = {s.id: s for s in Subject.objects.filter(id__in=ids, is_active=True)}
    with_tests = {e.subject_id for e in pool if e.test_type == Exam.TestType.SECTIONAL}
    with_questions = set(Question.objects.filter(subject_id__in=found, is_active=True).values_list("subject_id", flat=True))
    return [found[i] for i in sorted(found) if i in with_tests or i in with_questions]


def subject_weights(subjects, stats) -> dict[int, float]:
    """How often each subject should come round: weak subjects more often, strong ones less (never less than about a quarter)."""
    weights = {}
    for s in subjects:
        acc = (stats.get(s.id) or {}).get("accuracy")
        weights[s.id] = UNKNOWN_ACCURACY_WEIGHT if acc is None else max(0.6, 0.6 + (100 - acc) / 50)
    return weights


def subject_cycle(subjects, weights):
    """An endless, evenly spread order of subjects in proportion to their weights (smooth weighted round robin)."""
    current = {s.id: 0.0 for s in subjects}
    by_id = {s.id: s for s in subjects}
    total = sum(weights.values())
    while subjects:
        for sid in current:
            current[sid] += weights[sid]
        pick = max(current, key=lambda sid: (current[sid], -sid))
        current[pick] -= total
        yield by_id[pick]


# --- building the list ----------------------------------------------------------------------

def generate_tasks(plan: StudyPlan, student, first_day, today=None, pool=None) -> list[StudyTask]:
    """
    The plan's tasks from `first_day` to the day before the exam. Each study day has a focus subject (weak ones come round more
    often): a subject test for it when there is one, else practice questions, with a full test now and then. A plan whose exam
    has no subjects falls back to a test a day plus the usual revision and practice.
    """
    pool = candidate_exams(student, plan.category) if pool is None else pool
    start = timezone.localtime(plan.created_at).date()
    subjects = plan_subjects(plan, student, pool)
    stats = subject_accuracy(student)
    cycle = subject_cycle(subjects, subject_weights(subjects, stats))
    sect = {s.id: [e for e in pool if e.test_type == Exam.TestType.SECTIONAL and e.subject_id == s.id] for s in subjects}
    full = [e for e in pool if e.test_type != Exam.TestType.SECTIONAL or not subjects] if subjects else list(pool)
    tasks: list[StudyTask] = []
    used_full, used_sect = 0, {s.id: 0 for s in subjects}
    days = (plan.exam_date - first_day).days

    def test_task(day, exam, again, prefix, subject=None):
        if prefix:  # "Maths: Drill 3", "Full test: Retake Mock 2"
            title = f"{prefix}{'Retake ' if again else ''}{exam.title}"[:200]
        else:
            title = f"{'Retake' if again else 'Take'}: {exam.title}"[:200]
        return StudyTask(plan=plan, date=day, kind=StudyTask.Kind.TEST, exam=exam, subject=subject, title=title, minutes=min(exam.duration_minutes, 120))

    for k in range(max(days, 0)):
        day = first_day + timedelta(days=k)
        n = (day - start).days  # the day's place in the whole plan, so a refreshed plan keeps its rhythm
        left = (plan.exam_date - day).days  # 1 means the day before the exam
        focus = next(cycle) if subjects and left > 1 else None
        subject_test = False
        if left > 1 and (plan.daily_minutes >= 45 or n % 2 == 0):
            own = sect.get(focus.id) if focus else None
            if own and n % 4 != 3:
                exam = own[used_sect[focus.id] % len(own)]
                tasks.append(test_task(day, exam, used_sect[focus.id] >= len(own), f"{focus.name}: ", focus))
                used_sect[focus.id] += 1
                subject_test = True
            elif full:
                exam = full[used_full % len(full)]
                tasks.append(test_task(day, exam, used_full >= len(full), "Full test: " if subjects else ""))
                used_full += 1
            elif own:
                exam = own[used_sect[focus.id] % len(own)]
                tasks.append(test_task(day, exam, used_sect[focus.id] >= len(own), f"{focus.name}: ", focus))
                used_sect[focus.id] += 1
                subject_test = True
        title = "Light revision of your mistakes, then rest early" if left == 1 else "Revise the questions you got wrong"
        tasks.append(StudyTask(plan=plan, date=day, kind=StudyTask.Kind.REVISE, title=title, minutes=10 if plan.daily_minutes < 45 else 15))
        if left > 1:
            if focus and not subject_test:
                tasks.append(StudyTask(plan=plan, date=day, kind=StudyTask.Kind.PRACTICE, subject=focus, title=f"Practise {focus.name}", minutes=15))
            elif not focus and n % 3 == 2:
                tasks.append(StudyTask(plan=plan, date=day, kind=StudyTask.Kind.PRACTICE, title="Practise your weak topics", minutes=15))
        if left > 1 and plan.daily_minutes >= 60 and n % 4 == 3:
            tasks.append(StudyTask(plan=plan, date=day, kind=StudyTask.Kind.LISTEN, title="Listen to explanations (Hindi audio)", minutes=15))
    return tasks


def build_tasks(plan: StudyPlan, student, today=None):
    """Creates the plan's tasks, one block of work per day from today until the day before the exam."""
    today = today or timezone.localdate()
    tasks = generate_tasks(plan, student, today, today)
    tasks.append(StudyTask(plan=plan, date=plan.exam_date, kind=StudyTask.Kind.EXAM_DAY, title=f"Exam day: {plan.target_name}", minutes=0))
    StudyTask.objects.bulk_create(tasks)
    return len(tasks)


def refresh(plan: StudyPlan, today=None) -> dict:
    """
    Plans the days after today again with the student's latest results, so subjects that have become weak come round more often.
    Today's tasks and everything already ticked off stay as they are.
    """
    today = today or timezone.localdate()
    first = today + timedelta(days=1)
    removed = plan.tasks.filter(done=False, date__gte=first).exclude(kind=StudyTask.Kind.EXAM_DAY).delete()[0]
    fresh = generate_tasks(plan, plan.student, first, today)
    StudyTask.objects.bulk_create(fresh)
    return {"replanned": len(fresh), "removed": removed}


def link_for(task: StudyTask) -> str:
    if task.kind == StudyTask.Kind.TEST and task.exam_id:
        return f"/student/tests/{task.exam.public_id}"
    if task.kind == StudyTask.Kind.PRACTICE and task.subject_id:
        return f"/student/practice?subject={task.subject_id}&name={quote(task.subject.name)}"
    return {"revise": "/student/revise", "practice": "/student/practice", "listen": "/student/listen"}.get(task.kind, "")


def sync_done(plan: StudyPlan, tasks):
    """Ticks off, by itself, the test tasks the student has taken and the practice tasks they have done on that day."""
    since = timezone.localtime(plan.created_at).date()
    open_tests = [t for t in tasks if not t.done and t.kind == StudyTask.Kind.TEST and t.exam_id]
    changed = []
    if open_tests:
        taken = {}
        rows = Attempt.objects.filter(student=plan.student, status=Attempt.Status.SUBMITTED, exam_id__in={t.exam_id for t in open_tests},
                                      submitted_at__isnull=False)
        for a in rows.order_by("submitted_at"):
            if timezone.localtime(a.submitted_at).date() >= since:
                taken.setdefault(a.exam_id, a.submitted_at)
        for t in open_tests:
            if t.exam_id in taken:
                t.done, t.done_at = True, taken[t.exam_id]
                changed.append(t)
    open_practice = [t for t in tasks if not t.done and t.kind == StudyTask.Kind.PRACTICE and t.date <= timezone.localdate()]
    if open_practice:
        days = {}
        rows = Attempt.objects.filter(student=plan.student, status=Attempt.Status.SUBMITTED, exam__is_practice=True, submitted_at__isnull=False)
        for a in rows:
            days.setdefault(timezone.localtime(a.submitted_at).date(), a.submitted_at)
        for t in open_practice:
            if t.date in days:
                t.done, t.done_at = True, days[t.date]
                changed.append(t)
    if changed:
        StudyTask.objects.bulk_update(changed, ["done", "done_at"])


def task_row(t: StudyTask, today) -> dict:
    return {
        "id": t.id, "date": t.date.isoformat(), "kind": t.kind, "title": t.title, "minutes": t.minutes, "done": t.done,
        "link": link_for(t), "overdue": (not t.done) and t.date < today and t.kind != StudyTask.Kind.EXAM_DAY,
        "subject": t.subject.name if t.subject_id else None, "subject_id": t.subject_id,
    }


def subject_progress(plan: StudyPlan, tasks, today) -> list[dict]:
    """One row per subject in the plan: the student's accuracy so far, and how much of that subject's work in the plan is done."""
    stats = subject_accuracy(plan.student)
    rows: dict[int, dict] = {}
    for t in tasks:
        if not t.subject_id:
            continue
        r = rows.setdefault(t.subject_id, {"id": t.subject_id, "name": t.subject.name, "tasks": 0, "done": 0, "tests": 0, "tests_done": 0, "next_date": None})
        r["tasks"] += 1
        r["done"] += 1 if t.done else 0
        if t.kind == StudyTask.Kind.TEST:
            r["tests"] += 1
            r["tests_done"] += 1 if t.done else 0
        if not t.done and t.date >= today and (r["next_date"] is None or t.date.isoformat() < r["next_date"]):
            r["next_date"] = t.date.isoformat()
    for sid, r in rows.items():
        s = stats.get(sid) or {}
        r["answered"] = s.get("answered", 0)
        r["accuracy"] = s.get("accuracy")
        r["weak"] = bool(r["answered"] >= MIN_ANSWERED_FOR_WEAK and r["accuracy"] is not None and r["accuracy"] < WEAK_BELOW_PERCENT)
    # Weakest first, subjects with no answers yet after the ones we know, then by name.
    return sorted(rows.values(), key=lambda r: (r["accuracy"] is None, r["accuracy"] if r["accuracy"] is not None else 0, r["name"]))


def payload(plan: StudyPlan, today=None) -> dict:
    today = today or timezone.localdate()
    tasks = list(plan.tasks.select_related("exam", "subject"))
    sync_done(plan, tasks)
    due = [t for t in tasks if t.kind != StudyTask.Kind.EXAM_DAY]
    total, done = len(due), sum(1 for t in due if t.done)
    overdue = [t for t in due if not t.done and t.date < today]
    upcoming_days = sorted({t.date for t in tasks if today < t.date <= today + timedelta(days=7)})
    return {
        "plan": {
            "target_name": plan.target_name, "exam_date": plan.exam_date.isoformat(), "daily_minutes": plan.daily_minutes,
            "category": plan.category.public_id if plan.category_id else None, "days_left": max((plan.exam_date - today).days, 0),
            "total": total, "done": done, "percent": round(done * 100 / total) if total else 0, "overdue_count": len(overdue),
            "finished": plan.exam_date < today,
        },
        "today": [task_row(t, today) for t in tasks if t.date == today],
        "overdue": [task_row(t, today) for t in overdue],
        "upcoming": [{"date": d.isoformat(), "tasks": [task_row(t, today) for t in tasks if t.date == d]} for d in upcoming_days],
        "subjects": subject_progress(plan, tasks, today),
    }


def reschedule(plan: StudyPlan, today=None) -> dict:
    """
    Catches a student up after missed days. Missed revision and practice are dropped (they come back every few days anyway); a missed
    test moves to the first coming day that has no test, or is added to the last days before the exam if every day is taken.
    """
    today = today or timezone.localdate()
    missed = list(plan.tasks.filter(done=False, date__lt=today).exclude(kind=StudyTask.Kind.EXAM_DAY))
    moved = dropped = 0
    last_day = plan.exam_date - timedelta(days=2)
    for t in missed:
        if t.kind != StudyTask.Kind.TEST:
            t.delete()
            dropped += 1
            continue
        if last_day < today:
            t.delete()
            dropped += 1
            continue
        busy = {d for d in plan.tasks.filter(kind=StudyTask.Kind.TEST, date__gte=today).values_list("date", flat=True)}
        day = today
        while day <= last_day and day in busy:
            day += timedelta(days=1)
        t.date = day if day <= last_day else last_day
        t.save(update_fields=["date"])
        moved += 1
    return {"moved": moved, "dropped": dropped}
