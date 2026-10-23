from django.core.management.base import BaseCommand

from notifications.services import send_weekly_summaries


class Command(BaseCommand):
    help = "Sends each active student a note about their last seven days. Run it once a week (cron), for example on Sunday evening."

    def handle(self, *args, **options):
        r = send_weekly_summaries()
        self.stdout.write(f"{r['notified']} summary(ies) sent, {r['emailed']} by email.")
