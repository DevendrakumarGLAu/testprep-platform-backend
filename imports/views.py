import csv
import io

from django.http import HttpResponse
from django.shortcuts import get_object_or_404
from django.db.models import Q
from rest_framework import mixins, serializers, status, viewsets
from rest_framework.decorators import action
from rest_framework.parsers import FormParser, JSONParser, MultiPartParser
from rest_framework.response import Response
from rest_framework.views import APIView

from accounts.models import log_activity
from accounts.permissions import IsAdminOrSuperAdmin

from . import parsers, pipeline
from .models import ImportJob, ImportRow

SOURCES = [
    {"id": "csv", "label": "CSV", "extensions": [".csv"], "available": True, "note": "UTF-8 encoded."},
    {"id": "excel", "label": "Excel", "extensions": [".xlsx", ".xls"], "available": True,
     "note": "First sheet is read. Formulas are not evaluated."},
    {"id": "pdf", "label": "PDF", "extensions": [".pdf"], "available": True,
     "note": "Text-based PDFs only. Always review the extracted questions."},
    {"id": "web", "label": "Website", "extensions": [], "available": False,
     "note": "Planned. Website extraction is not part of this release."},
]


class RowSerializer(serializers.ModelSerializer):
    class Meta:
        model = ImportRow
        fields = [
            "id", "row_number", "raw", "normalized", "status", "errors", "duplicate_of", "duplicate_info",
            "selected", "result", "result_message", "question",
        ]


class JobSerializer(serializers.ModelSerializer):
    created_by_name = serializers.CharField(source="created_by.username", default=None, read_only=True)
    sample = serializers.SerializerMethodField()
    selected_count = serializers.SerializerMethodField()

    class Meta:
        model = ImportJob
        fields = [
            "id", "source_type", "filename", "status", "headers", "mapping", "defaults", "create_missing", "warnings",
            "total_rows", "valid_rows", "invalid_rows", "duplicate_rows", "imported_count", "failed_count",
            "skipped_count", "created_at", "completed_at", "created_by_name", "sample", "selected_count",
        ]

    def get_selected_count(self, job):
        return job.rows.filter(selected=True).count()

    def get_sample(self, job):
        return [r.raw for r in job.rows.all()[:5]]


class SourceListView(APIView):
    permission_classes = [IsAdminOrSuperAdmin]

    def get(self, request):
        return Response(SOURCES)


class TemplateView(APIView):
    permission_classes = [IsAdminOrSuperAdmin]

    def get(self, request):
        fmt = request.query_params.get("file", "csv")
        if fmt == "xlsx":
            from openpyxl import Workbook

            wb = Workbook()
            ws = wb.active
            ws.title = "Questions"
            ws.append(pipeline.TEMPLATE_HEADERS)
            ws.append(pipeline.TEMPLATE_EXAMPLE)
            buf = io.BytesIO()
            wb.save(buf)
            resp = HttpResponse(buf.getvalue(), content_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")
            resp["Content-Disposition"] = 'attachment; filename="question-import-template.xlsx"'
            return resp
        out = io.StringIO()
        w = csv.writer(out)
        w.writerow(pipeline.TEMPLATE_HEADERS)
        w.writerow(pipeline.TEMPLATE_EXAMPLE)
        resp = HttpResponse("﻿" + out.getvalue(), content_type="text/csv; charset=utf-8")
        resp["Content-Disposition"] = 'attachment; filename="question-import-template.csv"'
        return resp


class ImportViewSet(mixins.ListModelMixin, mixins.RetrieveModelMixin, viewsets.GenericViewSet):
    permission_classes = [IsAdminOrSuperAdmin]
    serializer_class = JobSerializer
    queryset = ImportJob.objects.select_related("created_by")
    filterset_fields = ["status", "source_type"]
    parser_classes = [JSONParser, FormParser, MultiPartParser]

    def create(self, request):
        """Step 1: upload. Reads the file into raw rows and proposes a column mapping."""
        source_hint = request.data.get("source")
        if source_hint == "web":
            return Response({"detail": "Website extraction is not available yet."}, status=status.HTTP_501_NOT_IMPLEMENTED)
        f = request.FILES.get("file")
        if not f:
            raise serializers.ValidationError({"file": "Choose a file to upload."})
        try:
            source, headers, rows, warnings = parsers.parse_file(f.name, f.read())
        except parsers.ParseError as e:
            raise serializers.ValidationError({"file": str(e)})

        job = ImportJob.objects.create(
            created_by=request.user, source_type=source, filename=f.name[:255], headers=headers,
            mapping=pipeline.guess_mapping(headers), warnings=warnings, total_rows=len(rows),
        )
        ImportRow.objects.bulk_create(ImportRow(job=job, row_number=i + 2 if source != "pdf" else i + 1, raw=r) for i, r in enumerate(rows))
        log_activity(request.user, "import.upload", job, f"{f.name}: {len(rows)} rows")
        return Response(JobSerializer(job).data, status=status.HTTP_201_CREATED)

    def destroy(self, request, pk=None):
        job = self.get_object()
        if job.status == ImportJob.Status.IMPORTED:
            return Response({"detail": "Completed imports are kept as a record."}, status=409)
        job.delete()
        return Response(status=status.HTTP_204_NO_CONTENT)

    def get_queryset(self):
        return super().get_queryset().prefetch_related("rows")

    @action(detail=True, methods=["post"])
    def process(self, request, pk=None):
        """Step 2: apply the mapping and defaults, validate and look for duplicates."""
        job = self.get_object()
        mapping = request.data.get("mapping") or job.mapping
        defaults = dict(request.data.get("defaults") or {})
        headers = set(job.headers)
        bad = [h for k, v in mapping.items() for h in (v if isinstance(v, list) else [v]) if h and h not in headers]
        if bad:
            raise serializers.ValidationError({"mapping": f"Unknown column(s): {', '.join(map(str, bad))}."})
        if not mapping.get("question"):
            raise serializers.ValidationError({"mapping": "Choose the column that holds the question text."})
        if len(mapping.get("options", [])) < 2:
            raise serializers.ValidationError({"mapping": "Choose at least two option columns."})
        try:
            job = pipeline.process_job(job, mapping, defaults, bool(request.data.get("create_missing")))
        except ValueError as e:
            raise serializers.ValidationError({"detail": str(e)})
        return Response(JobSerializer(job).data)

    @action(detail=True, methods=["get"])
    def rows(self, request, pk=None):
        job = self.get_object()
        qs = job.rows.all()
        if st := request.query_params.get("status"):
            qs = qs.filter(status=st)
        page = self.paginate_queryset(qs)
        return self.get_paginated_response(RowSerializer(page, many=True).data)

    @action(detail=True, methods=["patch"], url_path=r"rows/(?P<row_id>\d+)")
    def edit_row(self, request, pk=None, row_id=None):
        """Select or deselect a row, or fix its values and re-validate it."""
        job = self.get_object()
        if job.status == ImportJob.Status.IMPORTED:
            raise serializers.ValidationError({"detail": "This import is already complete."})
        row = get_object_or_404(ImportRow, pk=row_id, job=job)
        fixes = request.data.get("fixes")
        if fixes and row.normalized:
            row.raw = self._apply_fixes(job, row, fixes)
            ctx = pipeline.Lookup()
            norm, errors = pipeline.normalize_row(row.raw, job.mapping, job.defaults, ctx, job.create_missing)
            row.normalized, row.errors = norm, errors
            row.duplicate_of, row.duplicate_info = None, None
            if errors:
                row.status, row.selected = ImportRow.State.INVALID, False
            else:
                found = ctx.find_duplicate(norm["question"], norm["subject_id"])
                if found:
                    row.status, row.selected = ImportRow.State.DUPLICATE, False
                    row.duplicate_of_id = found[0]
                    row.duplicate_info = {"in_file": False, "question": found[0], "similarity": round(found[1], 2), "exact": found[2]}
                else:
                    row.status, row.selected = ImportRow.State.VALID, True
        if "selected" in request.data and row.status != ImportRow.State.INVALID:
            row.selected = bool(request.data["selected"])
        row.save()
        pipeline.recount(job)
        return Response({"row": RowSerializer(row).data, "job": JobSerializer(job).data})

    @staticmethod
    def _apply_fixes(job, row, fixes):
        """Writes edited values back into the row's raw cells, so the same mapping logic re-reads them."""
        raw = dict(row.raw)
        m = job.mapping
        simple = {"question": "question", "correct": "correct", "explanation": "explanation", "subject": "subject",
                  "topic": "topic", "difficulty": "difficulty", "marks": "marks", "negative_marks": "negative_marks"}
        for key, field in simple.items():
            if key in fixes and m.get(field):
                raw[m[field]] = str(fixes[key])
            elif key in fixes:
                # The column was not mapped (e.g. no answer column in a PDF); add one so it is remembered.
                col = f"fix_{field}"
                raw[col] = str(fixes[key])
                if col not in job.headers:
                    job.headers = [*job.headers, col]
                m[field] = col
                job.mapping = m
                job.save(update_fields=["headers", "mapping"])
        return raw

    @action(detail=True, methods=["post"])
    def select(self, request, pk=None):
        """Bulk selection: {"mode": "valid" | "none" | "valid_and_duplicates"}."""
        job = self.get_object()
        mode = request.data.get("mode")
        if mode not in ("valid", "none", "valid_and_duplicates"):
            raise serializers.ValidationError({"mode": "Use valid, none or valid_and_duplicates."})
        job.rows.update(selected=False)
        if mode == "valid":
            job.rows.filter(status=ImportRow.State.VALID).update(selected=True)
        elif mode == "valid_and_duplicates":
            job.rows.exclude(status=ImportRow.State.INVALID).update(selected=True)
        return Response(JobSerializer(job).data)

    @action(detail=True, methods=["post"])
    def commit(self, request, pk=None):
        """Step 4: save the selected rows."""
        job = self.get_object()
        if not job.rows.filter(selected=True).exists():
            raise serializers.ValidationError({"detail": "Select at least one row to import."})
        try:
            job = pipeline.commit_job(job, request.user)
        except ValueError as e:
            raise serializers.ValidationError({"detail": str(e)})
        log_activity(request.user, "import.commit", job, f"{job.imported_count} imported, {job.failed_count} failed")
        return Response(JobSerializer(job).data)

    @action(detail=True, methods=["get"], url_path="failed-report")
    def failed_report(self, request, pk=None):
        """CSV of every row that could not be imported, with the reason, in the original column layout."""
        job = self.get_object()
        bad = job.rows.filter(Q(status=ImportRow.State.INVALID) | Q(result=ImportRow.Result.FAILED))
        out = io.StringIO()
        w = csv.writer(out)
        w.writerow(["row", "problem", *job.headers])
        for r in bad.order_by("row_number"):
            problem = "; ".join(r.errors) if r.status == ImportRow.State.INVALID else r.result_message
            w.writerow([r.row_number, _safe(problem), *[_safe(r.raw.get(h, "")) for h in job.headers]])
        resp = HttpResponse("﻿" + out.getvalue(), content_type="text/csv; charset=utf-8")
        resp["Content-Disposition"] = f'attachment; filename="import-{job.id}-failed-rows.csv"'
        return resp


def _safe(v) -> str:
    """Stops spreadsheet apps from running a cell that starts with = + - @ as a formula."""
    s = str(v)
    return "'" + s if s[:1] in ("=", "+", "@") or (s[:1] == "-" and not s[1:2].isdigit()) else s
