"""
Purpose: Password hashing (scrypt), signed session cookies, and Fernet encryption helpers for secrets at rest.
Role: util
Patterns: structured-errors
Depends on: base64, hashlib, hmac, os, secrets, pathlib, cryptography
Exposes: hash_password, verify_password, generate_session_token, hash_session_token, sign_session_cookie, verify_session_cookie, load_or_create_secret_key, encrypt_secret, decrypt_secret
Tests: tests/test_auth_and_roles.py
Invariants: Never logs plaintext passwords, tokens, or secret keys; auto-generated local keyfiles always use 0600 permissions.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import os
import secrets
from pathlib import Path

from cryptography.fernet import Fernet, InvalidToken

SCRYPT_N = 2**14
SCRYPT_R = 8
SCRYPT_P = 1
SCRYPT_DKLEN = 32


def hash_password(password: str) -> str:
    """Hash a plaintext password using hashlib.scrypt with a random 16-byte per-user salt.

    Parameters:
        password: Plaintext password string.
    Returns:
        Encoded string `scrypt$<n>$<r>$<p>$<salt_b64>$<hash_b64>`.
    Side effects:
        None.
    """
    salt = secrets.token_bytes(16)
    derived = hashlib.scrypt(
        password.encode("utf-8"),
        salt=salt,
        n=SCRYPT_N,
        r=SCRYPT_R,
        p=SCRYPT_P,
        dklen=SCRYPT_DKLEN,
    )
    salt_b64 = base64.urlsafe_b64encode(salt).decode("ascii")
    hash_b64 = base64.urlsafe_b64encode(derived).decode("ascii")
    return f"scrypt${SCRYPT_N}${SCRYPT_R}${SCRYPT_P}${salt_b64}${hash_b64}"


def verify_password(password: str, stored_hash: str) -> bool:
    """Verify a plaintext password against a stored scrypt hash in constant time.

    Parameters:
        password: Candidate plaintext password.
        stored_hash: Persisted scrypt hash string.
    Returns:
        True if the password matches, otherwise False.
    Side effects:
        None.
    """
    try:
        scheme, n_str, r_str, p_str, salt_b64, expected_b64 = stored_hash.split("$", 5)
        if scheme != "scrypt":
            return False
        salt = base64.urlsafe_b64decode(salt_b64.encode("ascii"))
        expected = base64.urlsafe_b64decode(expected_b64.encode("ascii"))
        derived = hashlib.scrypt(
            password.encode("utf-8"),
            salt=salt,
            n=int(n_str),
            r=int(r_str),
            p=int(p_str),
            dklen=len(expected),
        )
        return hmac.compare_digest(derived, expected)
    except Exception:
        return False


def generate_session_token() -> str:
    """Generate a cryptographically secure URL-safe random session token."""
    return secrets.token_urlsafe(32)


def hash_session_token(token: str) -> str:
    """Return SHA-256 hex digest of a session token for database storage."""
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def _derive_fernet_key(master_secret: str) -> bytes:
    digest = hashlib.sha256(master_secret.encode("utf-8")).digest()
    return base64.urlsafe_b64encode(digest)


def sign_session_cookie(token: str, master_secret: str) -> str:
    """Attach an HMAC-SHA256 signature to a session token for cookie transport."""
    sig = hmac.new(
        master_secret.encode("utf-8"),
        token.encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()
    return f"{token}.{sig}"


def verify_session_cookie(cookie_value: str, master_secret: str) -> str | None:
    """Verify a signed session cookie value and return the underlying raw token if valid."""
    if not cookie_value or "." not in cookie_value:
        return None
    token, provided_sig = cookie_value.rsplit(".", 1)
    expected_sig = hmac.new(
        master_secret.encode("utf-8"),
        token.encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()
    if hmac.compare_digest(provided_sig, expected_sig):
        return token
    return None


def load_or_create_secret_key(data_dir: Path, env_secret_key: str = "") -> str:
    """Return the master encryption/signing key from env or an auto-generated 0600 local keyfile.

    Parameters:
        data_dir: Application data directory where `.secret_key` is stored if env is unset.
        env_secret_key: Optional explicit `APP_SECRET_KEY` override.
    Returns:
        Master secret string.
    Side effects:
        Creates `data_dir/.secret_key` with `0600` permissions if absent.
    """
    if env_secret_key and env_secret_key.strip():
        return env_secret_key.strip()

    data_dir.mkdir(parents=True, exist_ok=True)
    key_path = data_dir / ".secret_key"
    if key_path.exists():
        content = key_path.read_text(encoding="utf-8").strip()
        if content:
            return content

    generated = Fernet.generate_key().decode("ascii")
    fd = os.open(str(key_path), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        os.write(fd, (generated + "\n").encode("ascii"))
    finally:
        os.close(fd)
    os.chmod(key_path, 0o600)
    return generated


def encrypt_secret(plaintext: str, master_secret: str) -> str:
    """Encrypt a plaintext secret (e.g. openWeightGym API key) using Fernet."""
    if not plaintext:
        return ""
    fernet = Fernet(_derive_fernet_key(master_secret))
    return fernet.encrypt(plaintext.encode("utf-8")).decode("ascii")


def decrypt_secret(ciphertext: str, master_secret: str) -> str:
    """Decrypt a Fernet-encrypted secret string; returns empty string if invalid/empty."""
    if not ciphertext:
        return ""
    fernet = Fernet(_derive_fernet_key(master_secret))
    try:
        return fernet.decrypt(ciphertext.encode("ascii")).decode("utf-8")
    except InvalidToken:
        return ""
