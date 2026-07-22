"""Field-level encryption for sensitive credential storage.

A Fernet key is derived from `SESSION_SECRET` via HKDF-SHA256 with a
fixed context salt. This gives:

- **DB-only compromise doesn't leak creds.** A dumped SQLite file
  (backup theft, cloud snapshot, casual leak) is useless without
  `SESSION_SECRET` from the app's environment.
- **Zero new configuration.** We already require SESSION_SECRET to be
  set in prod for cookie signing; reusing it here doesn't add a
  moving part. The fixed salt keeps the derived keys domain-separated
  from any other use of the root secret.
- **Key rotation invalidates stored creds.** If you change
  SESSION_SECRET, every encrypted Wallabag credential becomes
  unrecoverable — users have to re-enter them. That's the correct
  behavior for a stolen-secret scenario.

Fernet uses AES-128-CBC + HMAC-SHA256 (authenticated symmetric
encryption) — the standard "give me a locked box" primitive from the
`cryptography` library.
"""

from __future__ import annotations

import base64

from cryptography.fernet import Fernet, InvalidToken
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.kdf.hkdf import HKDF

from .config import SESSION_SECRET

# Salt fixes the derivation so this key stays distinct from any other
# HKDF output that might use SESSION_SECRET as input. Bump the salt to
# force a re-encryption pass on all rows (also invalidates existing).
_HKDF_SALT = b"rssfriends/field-encryption/v1"
_HKDF_INFO = b"WallabagAccount credential fields"


def _derive_key() -> bytes:
    """32-byte URL-safe base64 Fernet key derived from SESSION_SECRET."""
    raw = HKDF(
        algorithm=hashes.SHA256(),
        length=32,
        salt=_HKDF_SALT,
        info=_HKDF_INFO,
    ).derive(SESSION_SECRET.encode("utf-8"))
    return base64.urlsafe_b64encode(raw)


_fernet: Fernet | None = None


def _fernet_instance() -> Fernet:
    """Lazy singleton — build the Fernet on first use so tests that
    reconfigure SESSION_SECRET before importing app.* still work."""
    global _fernet
    if _fernet is None:
        _fernet = Fernet(_derive_key())
    return _fernet


def encrypt(plaintext: str) -> str:
    """Encrypt a string; empty input passes through untouched so the
    default `""` values on new SQLModel rows don't get re-encrypted
    every time. Cipher output is ASCII (base64 URL-safe)."""
    if not plaintext:
        return ""
    return _fernet_instance().encrypt(plaintext.encode("utf-8")).decode("ascii")


def decrypt(ciphertext: str) -> str:
    """Decrypt a string previously produced by `encrypt`. Returns "" if
    the value is empty OR if it's plaintext (unencrypted legacy rows
    from before this module existed — Fernet tokens are always at
    least a few dozen bytes and start with a version byte, so real
    plaintext credentials are unambiguously distinguishable)."""
    if not ciphertext:
        return ""
    try:
        return _fernet_instance().decrypt(ciphertext.encode("ascii")).decode("utf-8")
    except (InvalidToken, ValueError, UnicodeError):
        # Legacy plaintext or a value written under a different key
        # (SESSION_SECRET rotated). Return "" so the caller re-prompts
        # for credentials instead of exposing plaintext by accident.
        return ""
