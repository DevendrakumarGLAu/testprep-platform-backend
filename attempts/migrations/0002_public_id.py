from django.db import migrations, models

import core.ids


def fill_public_ids(apps, schema_editor):
    Attempt = apps.get_model("attempts", "Attempt")
    for obj in Attempt.objects.all():
        obj.public_id = core.ids.new_public_id()
        obj.save(update_fields=["public_id"])


class Migration(migrations.Migration):
    dependencies = [("attempts", "0001_initial")]

    operations = [
        migrations.AddField("attempt", "public_id", models.CharField(max_length=24, null=True, editable=False)),
        migrations.RunPython(fill_public_ids, migrations.RunPython.noop),
        migrations.AlterField(
            "attempt", "public_id",
            models.CharField(max_length=24, unique=True, editable=False, default=core.ids.new_public_id),
        ),
    ]
