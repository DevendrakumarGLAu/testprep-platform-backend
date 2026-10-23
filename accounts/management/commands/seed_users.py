from django.core.management.base import BaseCommand

from accounts.models import Role, User

# Development-only credentials. Never use these outside a local machine.
SEEDS = [
    ("superadmin", "superadmin@example.com", Role.SUPER_ADMIN, "superadmin@123"),
    ("admin", "admin@example.com", Role.ADMIN, "admin123"),
    ("student", "student@example.com", Role.STUDENT, "student@123"),
]


class Command(BaseCommand):
    help = "Create development super admin, admin and student accounts (idempotent). Use --reset to reset their passwords."

    def add_arguments(self, parser):
        parser.add_argument("--reset", action="store_true", help="Reset the seed accounts' passwords to the development values.")

    def handle(self, *args, **opts):
        for username, email, role, password in SEEDS:
            user, created = User.objects.get_or_create(
                username=username, defaults={"email": email, "role": role, "first_name": username.title()}
            )
            if created or opts["reset"]:
                user.set_password(password)
                user.save()
            state = "created" if created else ("password reset" if opts["reset"] else "exists")
            self.stdout.write(f"{state:14} {username} ({role})")
        self.stdout.write("Development passwords: superadmin@123, admin123, student@123 (development only)")
