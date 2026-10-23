from django.core.management.base import BaseCommand

from notifications.services import send_start_reminders


class Command(BaseCommand):
    help = "Reminds students about tests that start within the next hour. Run it every 10 to 15 minutes (cron). Safe to run often: nobody gets the same reminder twice."

    def handle(self, *args, **options):
        n = send_start_reminders()
        self.stdout.write(f"{n} reminder(s) sent.")
