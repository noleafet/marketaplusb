import hashlib
import secrets

from .models import ApiClient

KEY_PREFIX_LEN = 12


def generate_key() -> str:
    return "mpb_" + secrets.token_urlsafe(30)


def hash_key(key: str) -> str:
    return hashlib.sha256(key.encode("utf-8")).hexdigest()


def create_client(name: str, rate_limit_per_min: int = 120) -> tuple[ApiClient, str]:
    """Create a client; the plaintext key is returned once and never stored."""
    key = generate_key()
    row = ApiClient.objects.create(
        name=name,
        key_prefix=key[:KEY_PREFIX_LEN],
        key_hash=hash_key(key),
        rate_limit_per_min=rate_limit_per_min,
    )
    return row, key
