"""PDF reports. This is the only export format for student reports (no CSV or Excel)."""
import io
from xml.sax.saxutils import escape

from django.utils import timezone
from reportlab.lib import colors
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.lib.units import mm
from reportlab.platypus import KeepTogether, Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle

from .stats import name_of

BRAND = colors.HexColor("#1a56a0")
LIGHT = colors.HexColor("#eef3fa")
GRID = colors.HexColor("#c9d3e0")

_base = getSampleStyleSheet()
H1 = ParagraphStyle("H1", parent=_base["Title"], fontSize=20, textColor=BRAND, alignment=0, spaceAfter=2)
H2 = ParagraphStyle("H2", parent=_base["Heading2"], fontSize=13, textColor=BRAND, spaceBefore=14, spaceAfter=6)
BODY = ParagraphStyle("Body", parent=_base["BodyText"], fontSize=9.5, leading=13)
SMALL = ParagraphStyle("Small", parent=BODY, fontSize=8, textColor=colors.HexColor("#555555"))
CELL = ParagraphStyle("Cell", parent=BODY, fontSize=8.5, leading=11)


def p(text, style=BODY):
    return Paragraph(escape(str(text)), style)


def fmt(v, suffix=""):
    return "-" if v is None else f"{v}{suffix}"


def fmt_dt(dt):
    return timezone.localtime(dt).strftime("%d %b %Y, %H:%M") if dt else "-"


def fmt_seconds(s):
    return "-" if s is None else f"{s // 60}m {s % 60:02d}s"


HEAD = ParagraphStyle("Head", parent=CELL, textColor=colors.white, fontName="Helvetica-Bold")


def table(rows, widths, header=True):
    cells = [
        [Paragraph(escape(c), HEAD if header and i == 0 else CELL) if isinstance(c, str) else c for c in r]
        for i, r in enumerate(rows)
    ]
    t = Table(cells, colWidths=widths, repeatRows=1 if header else 0)
    style = [
        ("GRID", (0, 0), (-1, -1), 0.4, GRID),
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ("TOPPADDING", (0, 0), (-1, -1), 3),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 3),
    ]
    if header:
        style += [("BACKGROUND", (0, 0), (-1, 0), BRAND), ("ROWBACKGROUNDS", (0, 1), (-1, -1), [colors.white, LIGHT])]
    t.setStyle(TableStyle(style))
    return t


def stat_grid(items, width=170 * mm):
    """A row of label/value tiles."""
    n = len(items)
    cells = [[Paragraph(f"<font size=15 color='#1a56a0'><b>{escape(str(v))}</b></font><br/>"
                        f"<font size=8 color='#555555'>{escape(label)}</font>", BODY) for label, v in items]]
    t = Table(cells, colWidths=[width / n] * n)
    t.setStyle(TableStyle([
        ("BOX", (0, 0), (-1, -1), 0.4, GRID), ("INNERGRID", (0, 0), (-1, -1), 0.4, GRID),
        ("BACKGROUND", (0, 0), (-1, -1), LIGHT), ("TOPPADDING", (0, 0), (-1, -1), 6), ("BOTTOMPADDING", (0, 0), (-1, -1), 6),
    ]))
    return t


def _build(story, title, user) -> bytes:
    buf = io.BytesIO()
    generated = f"Generated {timezone.localtime().strftime('%d %b %Y, %H:%M')} by {name_of(user)}"

    def footer(canvas, doc):
        canvas.saveState()
        canvas.setFont("Helvetica", 8)
        canvas.setFillColor(colors.HexColor("#777777"))
        canvas.drawString(20 * mm, 10 * mm, f"TestPrep · {title} · {generated}")
        canvas.drawRightString(A4[0] - 20 * mm, 10 * mm, f"Page {doc.page}")
        canvas.setStrokeColor(GRID)
        canvas.line(20 * mm, 13 * mm, A4[0] - 20 * mm, 13 * mm)
        canvas.restoreState()

    doc = SimpleDocTemplate(buf, pagesize=A4, leftMargin=20 * mm, rightMargin=20 * mm, topMargin=18 * mm,
                            bottomMargin=20 * mm, title=title, author="TestPrep")
    doc.build(story, onFirstPage=footer, onLaterPages=footer)
    return buf.getvalue()


def _subject_table(subjects):
    if not subjects:
        return p("No completed attempts yet.", SMALL)
    rows = [["Subject", "Correct", "Incorrect", "Unanswered", "Accuracy", "Marks"]]
    for s in subjects:
        rows.append([s["subject"], str(s["correct"]), str(s["incorrect"]), str(s["unanswered"]),
                     f"{s['accuracy']}%", f"{s['marks']} / {s['max_marks']}"])
    return table(rows, [48 * mm, 20 * mm, 22 * mm, 25 * mm, 22 * mm, 33 * mm])


def _history_table(history, limit=None):
    if not history:
        return p("No attempts yet.", SMALL)
    rows = [["Test", "#", "Started", "Status", "Score", "Result", "Time"]]
    for h in history[:limit]:
        score = f"{h['score']} / {h['total_marks']} ({h['percentage']}%)" if h["score"] is not None else "-"
        result = "-" if h["passed"] is None else ("Passed" if h["passed"] else "Not passed")
        status = "In progress" if h["status"] == "in_progress" else ("Auto-submitted" if h["auto_submitted"] else "Submitted")
        rows.append([h["exam_title"], str(h["attempt_number"]), fmt_dt(h["started_at"]), status, score, result,
                     fmt_seconds(h["time_taken_seconds"])])
    return table(rows, [42 * mm, 8 * mm, 28 * mm, 22 * mm, 33 * mm, 16 * mm, 16 * mm])


def student_report(student, perf, batches, user) -> bytes:
    story = [
        p("Student performance report", H1),
        p(f"{name_of(student)} · {student.email}", BODY),
        p(f"Username {student.username} · Registered {fmt_dt(student.date_joined)} · "
          f"Status {'Active' if student.is_active else 'Inactive'} · Batches: {', '.join(batches) or 'none'}", SMALL),
        Spacer(1, 8),
        stat_grid([
            ("Completed attempts", perf["attempts_count"]),
            ("Average score", fmt(perf["average_percentage"], "%")),
            ("Best score", fmt(perf["best_percentage"], "%")),
            ("Accuracy", fmt(perf["accuracy_percent"], "%")),
            ("Pass rate", fmt(perf["pass_rate"], "%")),
        ]),
        p("Subject-wise performance", H2),
        _subject_table(perf["subjects"]),
        p("Attempt history", H2),
        _history_table(perf["history"]),
    ]
    return _build(story, f"Student report: {name_of(student)}", user)


def consolidated_students(items, user) -> bytes:
    story = [p("Consolidated student report", H1), p(f"{len(items)} student(s)", SMALL), Spacer(1, 8)]
    rows = [["Student", "Status", "Attempts", "Average", "Best", "Accuracy", "Pass rate"]]
    for s, perf in items:
        rows.append([f"{name_of(s)} ({s.email})", "Active" if s.is_active else "Inactive", str(perf["attempts_count"]),
                     fmt(perf["average_percentage"], "%"), fmt(perf["best_percentage"], "%"),
                     fmt(perf["accuracy_percent"], "%"), fmt(perf["pass_rate"], "%")])
    story.append(table(rows, [62 * mm, 16 * mm, 16 * mm, 16 * mm, 14 * mm, 18 * mm, 18 * mm]))
    story.append(p("Per-student detail", H2))
    for s, perf in items:
        story.append(KeepTogether([
            p(f"{name_of(s)} ({s.email})", ParagraphStyle("Name", parent=BODY, fontName="Helvetica-Bold", spaceBefore=8)),
            _subject_table(perf["subjects"]),
            Spacer(1, 4),
            _history_table([h for h in perf["history"] if h["status"] == "submitted"], limit=8),
        ]))
    return _build(story, "Consolidated student report", user)


def consolidated_exams(items, user) -> bytes:
    story = [p("Consolidated test report", H1), p(f"{len(items)} test(s)", SMALL)]
    for exam, perf in items:
        block = [
            p(exam.title, H2),
            p(f"{exam.get_status_display()} · {exam.duration_minutes} min · pass mark {exam.passing_marks} of {exam.total_marks}", SMALL),
            Spacer(1, 4),
            stat_grid([
                ("Attempts", perf["attempts"]), ("Students", perf["students"]),
                ("Average", fmt(perf["average_score"])), ("Highest", fmt(perf["highest_score"])),
                ("Lowest", fmt(perf["lowest_score"])), ("Pass rate", fmt(perf["pass_rate"], "%")),
            ]),
            Spacer(1, 6),
        ]
        if perf["rows"]:
            rows = [["Student", "Attempts", "Best score", "%", "Result"]]
            for r in perf["rows"][:200]:
                rows.append([f"{r['name']} ({r['email']})", str(r["attempts"]), f"{r['best_score']} / {r['total_marks']}",
                             fmt(r["percentage"], "%"), "Passed" if r["passed"] else "Not passed"])
            block.append(table(rows, [72 * mm, 18 * mm, 30 * mm, 18 * mm, 22 * mm]))
        else:
            block.append(p("No completed attempts yet.", SMALL))
        story.extend(block)
    return _build(story, "Consolidated test report", user)
