from django.core.management.base import BaseCommand

from coach.reminders import send_plan_reminders


class Command(BaseCommand):
    help = "Send each student with work left in today's study plan a reminder in the bell. Run it once each morning."

    def handle(self, *args, **options):
        self.stdout.write(f"Reminders sent: {send_plan_reminders()}")
