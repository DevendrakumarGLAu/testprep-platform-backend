"""
A small image captcha with no outside service.

`new_challenge()` draws a random 5-character code, stores only its hash with a random id (the token), and returns the
token with a PNG. `check()` consumes the token whether or not the answer is right, so one image allows one guess and a
solved image cannot be replayed. Challenges are kept in the database, so every server process sees them.
"""
import base64
import hashlib
import hmac
import io
import random
import secrets
from datetime import timedelta

from django.conf import settings
from django.utils import timezone
from PIL import Image, ImageDraw, ImageFilter, ImageFont
from rest_framework.exceptions import APIException

ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"  # no 0/O, 1/I/L to avoid unfair guesses
LENGTH = 5
LIFETIME = timedelta(minutes=5)
WIDTH, HEIGHT = 180, 56


class CaptchaError(APIException):
    status_code = 400
    default_detail = "Enter the characters shown in the picture."
    default_code = "captcha"


def _hash(code: str) -> str:
    return hmac.new(settings.SECRET_KEY.encode(), code.upper().encode(), hashlib.sha256).hexdigest()


def _font(size: int):
    for name in ("DejaVuSans-Bold.ttf", "arialbd.ttf", "Arial Bold.ttf"):
        try:
            return ImageFont.truetype(name, size)
        except OSError:
            continue
    return ImageFont.load_default(size=size)


def _render(code: str) -> bytes:
    rnd = random.SystemRandom()
    img = Image.new("RGB", (WIDTH, HEIGHT), (rnd.randint(235, 255), rnd.randint(235, 255), rnd.randint(235, 255)))
    d = ImageDraw.Draw(img)
    for _ in range(7):  # noise lines behind the text
        d.line([(rnd.randint(0, WIDTH), rnd.randint(0, HEIGHT)), (rnd.randint(0, WIDTH), rnd.randint(0, HEIGHT))],
               fill=(rnd.randint(120, 200),) * 3, width=rnd.randint(1, 2))
    font = _font(36)
    step = (WIDTH - 24) / LENGTH
    for i, ch in enumerate(code):
        tile = Image.new("RGBA", (48, 56), (0, 0, 0, 0))
        ImageDraw.Draw(tile).text((8, 6), ch, font=font, fill=(rnd.randint(10, 90), rnd.randint(10, 90), rnd.randint(60, 140), 255))
        tile = tile.rotate(rnd.randint(-22, 22), resample=Image.BICUBIC, expand=False)
        img.paste(tile, (int(12 + i * step) - 4, rnd.randint(-3, 4)), tile)
    for _ in range(90):  # speckle
        d.point((rnd.randint(0, WIDTH - 1), rnd.randint(0, HEIGHT - 1)), fill=(rnd.randint(60, 180),) * 3)
    img = img.filter(ImageFilter.SMOOTH)
    out = io.BytesIO()
    img.save(out, "PNG")
    return out.getvalue()


def new_challenge() -> dict:
    from .models import CaptchaChallenge

    CaptchaChallenge.objects.filter(created_at__lt=timezone.now() - 2 * LIFETIME).delete()
    code = "".join(secrets.choice(ALPHABET) for _ in range(LENGTH))
    token = secrets.token_hex(16)
    CaptchaChallenge.objects.create(token=token, answer_hash=_hash(code))
    return {"enabled": True, "token": token, "image": "data:image/png;base64," + base64.b64encode(_render(code)).decode()}


def check(token, answer) -> None:
    """Raises CaptchaError unless `answer` matches the challenge `token`. The challenge is used up either way."""
    from .models import CaptchaChallenge

    token, answer = str(token or ""), str(answer or "").strip()
    if not token or not answer:
        raise CaptchaError("Enter the characters shown in the picture.")
    challenge = CaptchaChallenge.objects.filter(token=token).first()
    if challenge is None:
        raise CaptchaError("This picture has expired. A new one has been loaded; please try again.")
    expired = challenge.created_at < timezone.now() - LIFETIME
    ok = hmac.compare_digest(challenge.answer_hash, _hash(answer))
    challenge.delete()
    if expired:
        raise CaptchaError("This picture has expired. A new one has been loaded; please try again.")
    if not ok:
        raise CaptchaError("The characters do not match. A new picture has been loaded; please try again.")


def require(request) -> None:
    """Called by the login, register and change-password views."""
    if getattr(settings, "CAPTCHA_ENABLED", True):
        check(request.data.get("captcha_token"), request.data.get("captcha_answer"))
