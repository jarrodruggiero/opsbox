"""Hold browser sessions and CSRF tokens for the manager page.

The manager page has no accounts of its own. A browser earns a session by
signing in to Claude as the same account the box is signed in as. The check
is done elsewhere; this module only keeps the sessions that result.

A session is a random token held in an HttpOnly cookie. Only its SHA-256 hash
is stored, so a copy of the sessions file can't be used to sign in.

Every state-changing request carries a CSRF token, derived from the session
with an HMAC keyed by a secret that never leaves the box. A page on another
site can't forge one.

The files live in the opsbox volume, at mode 0600, and are written atomically,
so a crash mid-write never leaves a half-written file.
"""
from __future__ import annotations


import hashlib
import hmac
import json
import os
import secrets
import threading
import time
from pathlib import Path


def _write_private(path: Path, data: str) -> None:
    """Write *data* to *path* atomically with mode 0600."""
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    fd = os.open(str(tmp), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        os.write(fd, data.encode())
    finally:
        os.close(fd)
    os.replace(str(tmp), str(path))


def _hash(token: str) -> str:
    """Return the SHA-256 hex digest of *token*."""
    return hashlib.sha256(token.encode()).hexdigest()


class Sessions:
    """Manage browser sessions and CSRF tokens."""

    def __init__(self, directory: Path, ttl_seconds: int = 30 * 24 * 3600) -> None:
        self.path = directory / "sessions.json"
        self.secret_path = directory / "secret"
        self.ttl_seconds = ttl_seconds
        self._lock = threading.Lock()

    def _load(self) -> dict:
        """Return the sessions dict, dropping expired entries."""
        try:
            data = json.loads(self.path.read_text())
        except (FileNotFoundError, json.JSONDecodeError):
            data = {}
        now = int(time.time())
        return {h: v for h, v in data.items() if v.get("expires", 0) > now}

    def _save(self, data: dict) -> None:
        _write_private(self.path, json.dumps(data))

    def issue(self, label: str) -> str:
        """Create a new session and return its token."""
        token = secrets.token_urlsafe(32)
        now = int(time.time())
        with self._lock:
            data = self._load()
            data[_hash(token)] = {
                "label": label[:120],
                "created": now,
                "expires": now + self.ttl_seconds,
            }
            self._save(data)
        return token

    def valid(self, token: str | None) -> bool:
        """Return True if *token* is a currently valid session."""
        if not token or len(token) > 256:
            return False
        with self._lock:
            return _hash(token) in self._load()

    def revoke(self, token: str) -> None:
        """Remove the session identified by *token*."""
        with self._lock:
            data = self._load()
            data.pop(_hash(token), None)
            self._save(data)

    def revoke_all(self) -> None:
        """Remove all sessions."""
        with self._lock:
            self._save({})

    def list(self) -> list[dict]:
        """Return session info sorted by creation time. Never includes hashes."""
        with self._lock:
            data = self._load()
        return sorted(
            [{"label": v["label"], "created": v["created"], "expires": v["expires"]}
             for v in data.values()],
            key=lambda e: e["created"],
        )

    def _secret(self) -> bytes:
        """Read or create the HMAC secret (at least 32 bytes)."""
        # Stored as hex, so read back with fromhex: returning the raw file would
        # give a different key from the one the first call used.
        try:
            raw = bytes.fromhex(self.secret_path.read_text().strip())
            if len(raw) >= 32:
                return raw
        except (FileNotFoundError, ValueError):
            pass
        secret = secrets.token_bytes(32)
        _write_private(self.secret_path, secret.hex())
        return secret

    def csrf(self, token: str) -> str:
        """Return the CSRF value for *token*."""
        return hmac.new(self._secret(), token.encode(), "sha256").hexdigest()

    def check_csrf(self, token: str | None, value: str | None) -> bool:
        """Return True if *value* is a valid CSRF token for *token*."""
        if not token or not value:
            return False
        if not self.valid(token):
            return False
        return hmac.compare_digest(self.csrf(token), value)
