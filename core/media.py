import uuid
from pathlib import PurePosixPath

from django.core.files.storage import default_storage


def media_url(request, path: str | None) -> str | None:
    """
    The address of a stored file. Supabase (or any remote storage) gives a full address already; with local storage it is a path on
    this server, which is made absolute from the request.
    """
    if not path:
        return None
    url = default_storage.url(path)
    if url.startswith(("http://", "https://")):
        return url
    return request.build_absolute_uri(url if url.startswith("/") else "/" + url)


def unique_upload_path(folder: str, filename: str) -> str:
    """folder/<random>.<ext>: a name nobody else can have, so files are never overwritten and can be cached for ever."""
    ext = PurePosixPath(filename or "").suffix.lower()
    if ext not in {".jpg", ".jpeg", ".png", ".webp", ".gif"}:
        ext = ".png" if ext == "" else ext[:6]
    return f"{folder}/{uuid.uuid4().hex}{ext}"


MAX_IMAGE_SIDE = 1600
PICTURE_FORMATS = {"JPEG", "PNG", "WEBP", "GIF"}


def prepare_image(f):
    """
    Checks that an upload is a real JPG, PNG, WebP or GIF picture and returns the file to store. A picture wider or taller than
    1600 pixels is shrunk (a phone does not need more, and it loads much faster); anything smaller is stored exactly as uploaded.
    """
    from io import BytesIO

    from django.core.files.base import ContentFile
    from PIL import Image, ImageOps
    from rest_framework import serializers

    try:
        Image.open(f).verify()  # reject files that only claim to be pictures
        f.seek(0)
        img = Image.open(f)
        fmt = (img.format or "").upper()
    except Exception:
        raise serializers.ValidationError({"image": "Upload a valid image file."})
    if fmt not in PICTURE_FORMATS:
        raise serializers.ValidationError({"image": "Use a JPG, PNG, WebP or GIF picture."})
    if fmt == "GIF" or max(img.size) <= MAX_IMAGE_SIDE:
        f.seek(0)
        return f  # untouched (a GIF may be animated)
    img = ImageOps.exif_transpose(img)
    img.thumbnail((MAX_IMAGE_SIDE, MAX_IMAGE_SIDE))
    out = BytesIO()
    if fmt == "JPEG":
        img.convert("RGB").save(out, "JPEG", quality=85, optimize=True)
    elif fmt == "PNG":
        img.save(out, "PNG", optimize=True)
    else:
        img.save(out, "WEBP", quality=85)
    return ContentFile(out.getvalue(), name=getattr(f, "name", "image"))


def remove_question_image(question) -> None:
    """
    Takes the picture off a question. The file itself is only deleted when nothing else uses it: a student's past attempt keeps its own
    link to the picture it was shown, and a duplicated question may share the same file.
    """
    from attempts.models import AttemptQuestion
    from questionbank.models import Question

    field = question.image
    name = field.name if field else ""
    if not name:
        return
    in_use = AttemptQuestion.objects.filter(image_path=name).exists() or Question.objects.filter(image=name).exclude(pk=question.pk).exists()
    if not in_use:
        field.storage.delete(name)
