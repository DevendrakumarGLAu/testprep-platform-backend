import secrets


def new_public_id() -> str:
    """An unguessable 16-character identifier for use in URLs (96 bits of randomness)."""
    return secrets.token_urlsafe(12)
