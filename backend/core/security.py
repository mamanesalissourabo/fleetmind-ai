"""Mots de passe et jetons : rien de secret n'est stocké en clair."""

import base64
import hashlib
import hmac
import secrets

SCRYPT_N = 2**14
SCRYPT_R = 8
SCRYPT_P = 1
SALT_BYTES = 16


def _derive(password: str, salt: bytes) -> bytes:
    return hashlib.scrypt(password.encode(), salt=salt, n=SCRYPT_N, r=SCRYPT_R, p=SCRYPT_P)


def hash_password(password: str) -> str:
    salt = secrets.token_bytes(SALT_BYTES)
    return f"{base64.b64encode(salt).decode()}${base64.b64encode(_derive(password, salt)).decode()}"


def verify_password(password: str, stored: str) -> bool:
    salt, expected = (base64.b64decode(part) for part in stored.split("$"))
    return hmac.compare_digest(_derive(password, salt), expected)


# Utilisé quand le compte n'existe pas, pour que la durée ne révèle pas son existence.
DUMMY_HASH = hash_password(secrets.token_urlsafe(16))


def new_token() -> str:
    return secrets.token_urlsafe(32)


def hash_token(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()
