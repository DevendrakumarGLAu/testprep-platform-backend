"""The emails the platform sends: password reset and email verification. They need an email service (SMTP settings)."""
import secrets

from django.conf import settings
from django.contrib.auth.tokens import default_token_generator
from django.core import signing
from django.core.mail import send_mail
from django.utils.encoding import force_bytes, force_str
from django.utils.http import urlsafe_base64_decode, urlsafe_base64_encode

from .models import PlatformSettings, User

VERIFY_SALT = "verify-email"
VERIFY_MAX_AGE = 3 * 24 * 3600
TEMP_PASSWORD_ALPHABET = "abcdefghjkmnpqrstuvwxyzABCDEFGHJKLMNPQRSTUVWXYZ23456789"


def email_enabled() -> bool:
    """True when an email service is set up (EMAIL_HOST); without one the reset and verification links cannot be sent."""
    return bool(getattr(settings, "EMAIL_ENABLED", False))


def _name() -> str:
    return PlatformSettings.load().platform_name


def _send(user: User, subject: str, body: str) -> None:
    send_mail(subject, body, settings.DEFAULT_FROM_EMAIL, [user.email], fail_silently=False)


def reset_link(user: User) -> str:
    uid = urlsafe_base64_encode(force_bytes(user.pk))
    return f"{settings.FRONTEND_URL.rstrip('/')}/reset-password/{uid}/{default_token_generator.make_token(user)}"


def send_password_reset(user: User) -> None:
    hours = settings.PASSWORD_RESET_TIMEOUT // 3600
    _send(
        user, f"Reset your {_name()} password",
        f"Hello {user.first_name or user.username},\n\nSomeone asked to reset the password of your {_name()} account.\n"
        f"Open this link to choose a new password (it works for {hours} hour{'s' if hours != 1 else ''}):\n\n{reset_link(user)}\n\n"
        "If you did not ask for this, you can ignore this email: your password has not changed.\n",
    )


def user_from_reset(uid: str, token: str) -> User | None:
    """The user a valid, unused, unexpired reset link belongs to, or None."""
    try:
        user = User.objects.get(pk=force_str(urlsafe_base64_decode(uid)), is_active=True)
    except (User.DoesNotExist, ValueError, TypeError, OverflowError):
        return None
    return user if default_token_generator.check_token(user, token) else None


def verification_link(user: User) -> str:
    token = signing.dumps({"uid": user.pk, "email": user.email}, salt=VERIFY_SALT)
    return f"{settings.FRONTEND_URL.rstrip('/')}/verify-email/{token}"


def send_verification(user: User) -> None:
    _send(
        user, f"Confirm your email for {_name()}",
        f"Hello {user.first_name or user.username},\n\nPlease confirm that this is your email address by opening:\n\n{verification_link(user)}\n\n"
        "The link works for 3 days.\n",
    )


def user_from_verification(token: str) -> User | None:
    """The user a valid verification link belongs to. A link for an old email address no longer works."""
    try:
        data = signing.loads(token, salt=VERIFY_SALT, max_age=VERIFY_MAX_AGE)
        user = User.objects.get(pk=data["uid"], is_active=True)
    except (signing.BadSignature, User.DoesNotExist, KeyError, TypeError):
        return None
    return user if user.email == data.get("email") else None


def new_temporary_password() -> str:
    return "".join(secrets.choice(TEMP_PASSWORD_ALPHABET) for _ in range(10))


def end_all_sessions(user: User) -> None:
    """Signs the person out everywhere by blacklisting every refresh token they hold."""
    from rest_framework_simplejwt.token_blacklist.models import BlacklistedToken, OutstandingToken

    for token in OutstandingToken.objects.filter(user=user):
        BlacklistedToken.objects.get_or_create(token=token)
