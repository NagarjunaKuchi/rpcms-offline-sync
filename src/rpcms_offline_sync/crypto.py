"""Column-level encryption for the sensitive fields (ProGres ID, auth reference).

Values are stored as ``enc1:<Fernet token>``. Several keys may be configured (comma-separated): the first
encrypts, all can decrypt, which allows rotation. Values that do not start with ``enc1:`` (rows written before
encryption was switched on, and the plain ``SCRUBBED`` marker) are returned unchanged; ``reencrypt`` upgrades them.

The cipher is process-wide state set by ``configure`` (one process, one key set).
This protects stored values against someone reading the database or a backup. It does not protect against
whoever holds the key, and the key must live outside the database (environment, secrets manager).
"""
from __future__ import annotations

from cryptography.fernet import Fernet, InvalidToken, MultiFernet
from sqlalchemy import String
from sqlalchemy.types import TypeDecorator

PREFIX = "enc1:"
PLAIN = {"SCRUBBED"}  # markers kept readable so SQL can compare against them

_cipher: MultiFernet | None = None


class KeyError_(RuntimeError):
    pass


def configure(keys: str | None) -> None:
    """Set the process cipher from comma-separated Fernet keys; None/empty means no encryption."""
    global _cipher
    parts = [k.strip() for k in (keys or "").split(",") if k.strip()]
    if not parts:
        _cipher = None
        return
    try:
        _cipher = MultiFernet([Fernet(k.encode()) for k in parts])
    except (ValueError, TypeError) as e:
        raise KeyError_("RPCMS_SYNC_DATA_KEYS must be comma-separated Fernet keys "
                        "(generate with: python -c \"from cryptography.fernet import Fernet; "
                        "print(Fernet.generate_key().decode())\")") from e


def encrypt(value: str) -> str:
    if _cipher is None or value in PLAIN or value.startswith(PREFIX):
        return value
    return PREFIX + _cipher.encrypt(value.encode()).decode()


def decrypt(value: str) -> str:
    if not value.startswith(PREFIX):
        return value
    if _cipher is None:
        raise KeyError_("Stored value is encrypted but no RPCMS_SYNC_DATA_KEYS is configured")
    try:
        return _cipher.decrypt(value[len(PREFIX):].encode()).decode()
    except InvalidToken as e:
        raise KeyError_("Stored value cannot be decrypted with the configured keys") from e


def rotate(value: str) -> str:
    """Re-encrypt with the primary key (also encrypts legacy plaintext)."""
    return encrypt(decrypt(value))


class EncryptedString(TypeDecorator):
    impl = String
    cache_ok = True

    def process_bind_param(self, value, dialect):
        return None if value is None else encrypt(value)

    def process_result_value(self, value, dialect):
        return None if value is None else decrypt(value)
