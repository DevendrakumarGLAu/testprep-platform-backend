"""
Where uploaded files (question images, test thumbnails) are kept.

Render's disk is wiped on every deploy, so on the live site files go to Supabase Storage through its S3-compatible API. With none of the
SUPABASE_S3_* settings the files stay in the local media folder, which is what development uses.
"""

LOCAL = {"BACKEND": "django.core.files.storage.FileSystemStorage"}
STATIC = {"BACKEND": "django.contrib.staticfiles.storage.StaticFilesStorage"}


def public_base(endpoint: str, bucket: str, override: str = "") -> str:
    """
    The address people's browsers load a public file from, without the scheme:
    <project>.supabase.co/storage/v1/object/public/<bucket>
    """
    if override:
        return override.removeprefix("https://").removeprefix("http://").rstrip("/")
    root = endpoint.removeprefix("https://").removeprefix("http://").rstrip("/")
    root = root.removesuffix("/s3")  # .../storage/v1/s3 -> .../storage/v1
    return f"{root}/object/public/{bucket}"


def build_storages(env) -> dict:
    """The STORAGES setting for the given environment (a dict-like such as os.environ)."""
    endpoint = (env.get("SUPABASE_S3_ENDPOINT") or "").strip()
    if not endpoint:
        return {"default": LOCAL, "staticfiles": STATIC}
    bucket = (env.get("SUPABASE_BUCKET") or "question-images").strip()
    return {
        "default": {
            "BACKEND": "storages.backends.s3.S3Storage",
            "OPTIONS": {
                "bucket_name": bucket,
                "endpoint_url": endpoint,
                "access_key": env.get("SUPABASE_S3_ACCESS_KEY_ID", ""),
                "secret_key": env.get("SUPABASE_S3_SECRET_ACCESS_KEY", ""),
                "region_name": env.get("SUPABASE_S3_REGION", "us-east-1"),
                "addressing_style": "path",  # Supabase needs path-style addresses
                "signature_version": "s3v4",
                "querystring_auth": False,  # the bucket is public, so plain links work
                "custom_domain": public_base(endpoint, bucket, env.get("SUPABASE_PUBLIC_BASE", "")),
                "file_overwrite": False,  # never replace someone else's file with the same name
                "default_acl": None,  # Supabase has no ACLs
                # File names are random and never reused, so browsers and the CDN can keep them for a year.
                "object_parameters": {"CacheControl": "public, max-age=31536000, immutable"},
            },
        },
        "staticfiles": STATIC,
    }
