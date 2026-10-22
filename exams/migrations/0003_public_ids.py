from django.db import migrations, models

import core.ids


def fill_public_ids(apps, schema_editor):
    for name in ("Exam", "ExamCategory"):
        model = apps.get_model("exams", name)
        for obj in model.objects.all():
            obj.public_id = core.ids.new_public_id()
            obj.save(update_fields=["public_id"])


class Migration(migrations.Migration):
    dependencies = [("exams", "0002_exam_subject_exam_test_type_exam_year_and_more")]

    operations = [
        # Add as nullable first so existing rows can each get their own value, then make it unique and required.
        migrations.AddField("exam", "public_id", models.CharField(max_length=24, null=True, editable=False)),
        migrations.AddField("examcategory", "public_id", models.CharField(max_length=24, null=True, editable=False)),
        migrations.RunPython(fill_public_ids, migrations.RunPython.noop),
        migrations.AlterField(
            "exam", "public_id",
            models.CharField(max_length=24, unique=True, editable=False, default=core.ids.new_public_id),
        ),
        migrations.AlterField(
            "examcategory", "public_id",
            models.CharField(max_length=24, unique=True, editable=False, default=core.ids.new_public_id),
        ),
    ]
