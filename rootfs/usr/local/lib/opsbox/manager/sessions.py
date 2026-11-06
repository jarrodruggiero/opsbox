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
import tempfile
import threading
import time
from pathlib import Path


def _write_private(path: Path, data: str | bytes) -> None:
    """Write *data* to *path* atomically with mode 0600."""
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    # A name of its own per write (mkstemp makes it 0600), so two writes at
    # once can't rename each other's temporary file away.
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.", suffix=".tmp")
    try:
        with os.fdopen(fd, "wb") as file:
            file.write(data.encode() if isinstance(data, str) else data)
        os.replace(tmp, path)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise


# How much of a session's hash names it on the page. A prefix of a hash of 256
# random bits: enough to tell the rows apart, useless as a credential.
SESSION_ID_LENGTH = 16


def session_id(token: str) -> str:
    """The short id of the session *token* opens."""
    return _hash(token)[:SESSION_ID_LENGTH]


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
        self._secret_lock = threading.Lock()
        self._secret_value: bytes | None = None

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

    def revoke_id(self, session_id: str) -> None:
        """Remove the session a browser row on the page stands for."""
        with self._lock:
            data = self._load()
            for hashed in [h for h in data if h[:SESSION_ID_LENGTH] == session_id]:
                del data[hashed]
            self._save(data)

    def revoke_all(self) -> None:
        """Remove all sessions."""
        with self._lock:
            self._save({})

    def list(self) -> list[dict]:
        """Return session info sorted by creation time, with a short id that can't be used as a session."""
        with self._lock:
            data = self._load()
        return sorted(
            [{"id": h[:SESSION_ID_LENGTH], "label": v["label"], "created": v["created"], "expires": v["expires"]}
             for h, v in data.items()],
            key=lambda e: e["created"],
        )

    def _secret(self) -> bytes:
        """Read or create the HMAC secret (at least 32 bytes), once."""
        # Under a lock: two first requests at once would otherwise each make
        # a secret, and the loser's CSRF value would never verify.
        with self._secret_lock:
            if self._secret_value is None:
                # Stored as hex, so read back with fromhex: returning the raw
                # file would give a different key from the one first used.
                try:
                    raw = bytes.fromhex(self.secret_path.read_text().strip())
                    if len(raw) >= 32:
                        self._secret_value = raw
                except (FileNotFoundError, ValueError):
                    pass
            if self._secret_value is None:
                self._secret_value = secrets.token_bytes(32)
                _write_private(self.secret_path, self._secret_value.hex())
            return self._secret_value

    def csrf(self, token: str) -> str:
        """Return the CSRF value for *token*."""
        return hmac.new(self._secret(), token.encode(), "sha256").hexdigest()

    def check_csrf(self, token: str | None, value: str | None) -> bool:
        """Return True if *value* is a valid CSRF token for *token*."""
        if not token or not value:
            return False
        if not self.valid(token):
            return False
        # As bytes: compare_digest raises on a str with any non-ASCII
        # character, and the form field is whatever the request sent.
        return hmac.compare_digest(self.csrf(token).encode(), value.encode())
