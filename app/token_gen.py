import secrets
import string

_ALPHABET = string.ascii_letters + string.digits  # 62 chars → ~60 bits entropy at length 10


def generate_token(length: int = 10) -> str:
    """Return a URL-safe random token for one recipient-link pair."""
    return "".join(secrets.choice(_ALPHABET) for _ in range(length))
