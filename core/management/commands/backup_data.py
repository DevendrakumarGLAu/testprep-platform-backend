import gzip
from datetime import datetime
from pathlib import Path

from django.core.management import call_command
from django.core.management.base import BaseCommand


class Command(BaseCommand):
    help = (
        "Writes the data (users, questions, tests, attempts and so on) to a compressed JSON file you can keep or restore with "
        "'manage.py loaddata'. Sessions and refresh-token records are left out. For a full database copy use the database provider's backups as well."
    )

    def add_arguments(self, parser):
        parser.add_argument("--out", default="backups", help="folder for the file (default: backups)")

    def handle(self, *args, **options):
        folder = Path(options["out"])
        folder.mkdir(parents=True, exist_ok=True)
        path = folder / f"testprep-{datetime.now():%Y%m%d-%H%M%S}.json.gz"
        from io import StringIO

        buffer = StringIO()
        call_command(
            "dumpdata", "--natural-foreign", "--natural-primary", "--indent", "1",
            exclude=["sessions", "token_blacklist", "contenttypes", "auth.permission", "admin.logentry"], stdout=buffer,
        )
        with gzip.open(path, "wt", encoding="utf-8") as f:
            f.write(buffer.getvalue())
        self.stdout.write(self.style.SUCCESS(f"Backup written to {path} ({path.stat().st_size // 1024} KB)."))
