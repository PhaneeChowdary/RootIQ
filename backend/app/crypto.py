"""Encryption for secrets that the app saves: model API keys and data-source passwords and tokens.

The key comes from ROOTIQ_SECRET_KEY, or from backend/data/secret.key. The app makes that file the first time it
needs it, and only the owner can read it. Keep a copy of the key in a safe place: without it, the saved secrets cannot
be read and must be entered again.

An encrypted value starts with "enc:v1:". A value without that start is plain text from an older version. decrypt()
gives it back as it is, and the next save encrypts it."""

import os
import threading
from pathlib import Path

from cryptography.fernet import Fernet, InvalidToken

PREFIX = "enc:v1:"
_lock = threading.Lock()
_fernet: Fernet | None = None


def _key_file() -> Path:
    from .config import Settings

    return Path(Settings().data_dir) / "secret.key"


def _cipher() -> Fernet:
    global _fernet
    with _lock:
        if _fernet is None:
            key = os.environ.get("ROOTIQ_SECRET_KEY", "").strip()
            if not key:
                path = _key_file()
                if not path.exists():
                    path.parent.mkdir(parents=True, exist_ok=True)
                    path.write_bytes(Fernet.generate_key())
                    os.chmod(path, 0o600)
                key = path.read_text().strip()
            _fernet = Fernet(key.encode())
        return _fernet


def encrypt(value: str) -> str:
    if not value or value.startswith(PREFIX):
        return value
    return PREFIX + _cipher().encrypt(value.encode()).decode()


def decrypt(value: str) -> str:
    if not value or not value.startswith(PREFIX):
        return value  # plain text from an older version
    try:
        return _cipher().decrypt(value[len(PREFIX):].encode()).decode()
    except InvalidToken as exc:
        raise RuntimeError("A saved secret cannot be decrypted: the secret key changed. Enter the secret again.") from exc


def reset_for_tests(key: str | None = None) -> None:
    """Use another key. For tests only."""
    global _fernet
    with _lock:
        _fernet = Fernet(key.encode()) if key else None
