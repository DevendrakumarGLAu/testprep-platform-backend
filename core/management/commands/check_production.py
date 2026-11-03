"""
Looks over the settings of a server and says what is wrong before real people use it.

    python manage.py check_production

Each line starts with OK, WARN (works, but you should look) or FAIL (fix before launch). It exits with an error when any line fails,
so it can also run at the end of a deployment.
"""
import os

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError
from django.db import connection
from django.db.migrations.executor import MigrationExecutor

from accounts.models import Role, User

LOCAL_WORDS = ("localhost", "127.0.0.1", "0.0.0.0")  # noqa: S104 - words to look for, not an address to listen on
DEMO_ACCOUNTS = {"superadmin": "superadmin@123", "admin": "admin123", "student": "student@123"}  # what `seed_users` makes


def _local(value: str) -> bool:
    return any(w in value for w in LOCAL_WORDS)


def run_checks() -> list[tuple[str, str, str]]:
    """(level, what was checked, what to do about it) for every check."""
    out: list[tuple[str, str, str]] = []

    def add(level, what, advice=""):
        out.append((level, what, advice))

    if settings.DEBUG:
        add("FAIL", "Debug mode is on", "Set DJANGO_DEBUG=False. In debug mode errors show internal details to anyone.")
    else:
        add("OK", "Debug mode is off")

    key = settings.SECRET_KEY
    if len(key) < 32 or key.startswith(("insecure", "replace-with")):
        add("FAIL", "The secret key is a default or too short", "Set DJANGO_SECRET_KEY to a long random value (at least 32 characters).")
    else:
        add("OK", "The secret key is long and not a default")

    hosts = [h for h in settings.ALLOWED_HOSTS]
    if not hosts or all(_local(h) for h in hosts):
        add("FAIL", f"Allowed hosts are only local ({', '.join(hosts) or 'none'})", "Set DJANGO_ALLOWED_HOSTS to your real API address.")
    elif "*" in hosts:
        add("WARN", "Allowed hosts accept any address (*)", "List only your own API address in DJANGO_ALLOWED_HOSTS.")
    else:
        add("OK", f"Allowed hosts: {', '.join(hosts)}")

    cors = list(getattr(settings, "CORS_ALLOWED_ORIGINS", []))
    if not cors or all(_local(o) for o in cors):
        add("FAIL", "Only local web addresses may call the API", "Set CORS_ALLOWED_ORIGINS to the address of your website, for example https://yourapp.onrender.com.")
    elif any(o.startswith("http://") and not _local(o) for o in cors):
        add("WARN", "A web address in CORS_ALLOWED_ORIGINS uses http, not https", "Use https addresses.")
    else:
        add("OK", f"The API accepts calls from: {', '.join(cors)}")

    front = getattr(settings, "FRONTEND_URL", "")
    if _local(front):
        add("WARN", f"FRONTEND_URL points to this computer ({front})", "Set FRONTEND_URL to your real website address. Links in emails use it.")
    else:
        add("OK", f"FRONTEND_URL is {front}")

    if not settings.DEBUG and getattr(settings, "SECURE_SSL_REDIRECT", False) and getattr(settings, "SESSION_COOKIE_SECURE", False):
        add("OK", "HTTPS is enforced (redirect and secure cookies)")
    else:
        add("FAIL", "HTTPS is not enforced", "Leave DJANGO_HTTPS on (the default) and set DJANGO_BEHIND_PROXY=True when the host terminates HTTPS.")

    if connection.vendor == "sqlite":
        add("FAIL", "The database is a local SQLite file", "Set DATABASE_URL to your PostgreSQL (for example Supabase). A hosted service loses a local file when it redeploys.")
    else:
        add("OK", f"The database is {connection.vendor}")

    try:
        pending = [m for m, _ in MigrationExecutor(connection).migration_plan(MigrationExecutor(connection).loader.graph.leaf_nodes())]
    except Exception as e:  # noqa: BLE001 - the database may not be reachable at all
        add("FAIL", f"The database could not be read ({type(e).__name__})", "Check DATABASE_URL.")
    else:
        if pending:
            add("FAIL", f"{len(pending)} database change(s) have not been applied", "Run: python manage.py migrate")
        else:
            add("OK", "The database is up to date")

    backend = settings.STORAGES["default"]["BACKEND"]
    if "FileSystemStorage" in backend:
        add("WARN", "Uploaded pictures are stored on this server's own disk", "A redeploy erases them. Set the SUPABASE_S3_* settings so pictures are kept (then run: python manage.py check_storage).")
    else:
        add("OK", "Uploaded pictures go to remote storage")

    if getattr(settings, "EMAIL_ENABLED", False):
        add("OK", "Email is set up")
    else:
        add("WARN", "Email is not set up", "Forgotten passwords then need an administrator: set the support phone in Platform settings so people know who to ask.")

    if os.getenv("SENTRY_DSN") or _sentry_on():
        add("OK", "Error reports go to Sentry")
    else:
        add("WARN", "Error reporting is off", "Create a free Sentry project and set SENTRY_DSN, so you hear about crashes before your users tell you.")

    weak = [u.username for u in User.objects.filter(username__in=DEMO_ACCOUNTS, is_active=True) if u.check_password(DEMO_ACCOUNTS[u.username])]
    if weak:
        add("FAIL", f"Demo accounts with their known passwords exist: {', '.join(sorted(weak))}", "Delete them or set strong passwords. Never run seed_users in production.")
    else:
        add("OK", "No demo account has a known password")

    if User.objects.filter(role=Role.SUPER_ADMIN, is_active=True).count() == 0:
        add("FAIL", "There is no active Super Admin", "Create one with: python manage.py createsuperuser")
    elif User.objects.filter(role__in=[Role.SUPER_ADMIN, Role.ADMIN], is_active=True).count() < 2:
        add("WARN", "Only one administrator exists", "Create a second one, so a forgotten admin password does not lock you out.")
    else:
        add("OK", "More than one administrator exists")

    return out


def _sentry_on() -> bool:
    try:
        import sentry_sdk

        return sentry_sdk.get_client().is_active()
    except Exception:  # noqa: BLE001
        return False


class Command(BaseCommand):
    help = "Checks the server's settings for a real launch and lists what to fix."

    def handle(self, *args, **options):
        results = run_checks()
        styles = {"OK": self.style.SUCCESS, "WARN": self.style.WARNING, "FAIL": self.style.ERROR}
        for level, what, advice in results:
            self.stdout.write(styles[level](f"{level:4}  {what}"))
            if advice:
                self.stdout.write(f"      -> {advice}")
        counts = {lv: sum(1 for r in results if r[0] == lv) for lv in ("OK", "WARN", "FAIL")}
        self.stdout.write(f"\n{counts['OK']} fine, {counts['WARN']} to look at, {counts['FAIL']} to fix.")
        if counts["FAIL"]:
            raise CommandError("Fix the lines marked FAIL before launch.")
