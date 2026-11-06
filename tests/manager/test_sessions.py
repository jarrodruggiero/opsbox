"""Tests for opsbox.manager.sessions."""
from __future__ import annotations

import hashlib
import json
import pathlib
import time


from opsbox.manager.sessions import Sessions


def test_issued_token_is_valid_and_others_are_not(tmp_path: pathlib.Path) -> None:
    """issue() returns a token that is valid; other values are not."""
    s = Sessions(tmp_path / "m")
    token = s.issue("x")
    assert s.valid(token)
    assert not s.valid("nope")
    assert not s.valid(None)
    assert not s.valid("")
    assert not s.valid("a" * 300)


def test_only_the_hash_is_stored(tmp_path: pathlib.Path) -> None:
    """After issue(), the raw token does not appear in sessions.json, but its hash does."""
    s = Sessions(tmp_path / "m")
    token = s.issue("x")
    text = (tmp_path / "m" / "sessions.json").read_text()
    assert token not in text
    assert hashlib.sha256(token.encode()).hexdigest() in text


def test_files_are_private(tmp_path: pathlib.Path) -> None:
    """sessions.json and secret are mode 0o600, and the directory is 0o700."""
    s = Sessions(tmp_path / "m")
    s.csrf(s.issue("x"))  # the secret is made on first use
    assert (tmp_path / "m" / "sessions.json").stat().st_mode & 0o777 == 0o600
    assert (tmp_path / "m" / "secret").stat().st_mode & 0o777 == 0o600
    assert (tmp_path / "m").stat().st_mode & 0o777 == 0o700


def test_expired_sessions_are_gone(tmp_path: pathlib.Path) -> None:
    """A token is valid immediately after issue, but not after ttl_seconds."""
    s = Sessions(tmp_path / "m", ttl_seconds=1)
    token = s.issue("x")
    assert s.valid(token)
    time.sleep(1.5)
    assert not s.valid(token)


def test_revoke_and_revoke_all(tmp_path: pathlib.Path) -> None:
    """revoke removes one token; revoke_all removes all."""
    s = Sessions(tmp_path / "m")
    t1 = s.issue("a")
    t2 = s.issue("b")
    s.revoke(t1)
    assert not s.valid(t1)
    assert s.valid(t2)
    s.revoke_all()
    assert not s.valid(t1)
    assert not s.valid(t2)


def test_list_never_shows_hashes(tmp_path: pathlib.Path) -> None:
    """list() never contains a hash or the raw token."""
    s = Sessions(tmp_path / "m")
    token = s.issue("x")
    items = s.list()
    text = json.dumps(items)
    assert hashlib.sha256(token.encode()).hexdigest() not in text
    assert token not in text


def test_csrf_matches_only_its_session(tmp_path: pathlib.Path) -> None:
    """check_csrf is True only for the correct (token, value) pair."""
    s = Sessions(tmp_path / "m")
    t = s.issue("x")
    other = s.issue("y")
    assert s.check_csrf(t, s.csrf(t))
    assert not s.check_csrf(t, s.csrf(other))
    assert not s.check_csrf(t, "")
    assert not s.check_csrf("nope", s.csrf("nope"))


def test_csrf_secret_survives_a_restart(tmp_path: pathlib.Path) -> None:
    """A new Sessions over the same directory gives the same csrf(t)."""
    s1 = Sessions(tmp_path / "m")
    t = s1.issue("x")
    val1 = s1.csrf(t)
    s2 = Sessions(tmp_path / "m")
    val2 = s2.csrf(t)
    assert val1 == val2


def test_write_is_atomic(tmp_path: pathlib.Path) -> None:
    """After issue(), no *.tmp file is left in the directory."""
    s = Sessions(tmp_path / "m")
    s.issue("x")
    files = list(tmp_path.rglob("*.tmp"))
    assert len(files) == 0
