from django.core.management.base import BaseCommand

from attempts.services import expire_overdue


class Command(BaseCommand):
    help = "Submit and score every in-progress attempt whose deadline has passed. Run every minute from a scheduler."

    def handle(self, *args, **options):
        n = expire_overdue()
        self.stdout.write(f"Auto-submitted {n} expired attempt(s).")
