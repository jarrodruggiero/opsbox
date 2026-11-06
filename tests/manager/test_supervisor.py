"""Tests for the RemoteControl supervisor."""
from __future__ import annotations

import os
import pathlib
import time

import pytest

from opsbox.manager.supervisor import Console, RemoteControl


def until(predicate, timeout=10):
    """Poll predicate every 0.05 s until True or timeout, then fail."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.05)
    raise AssertionError(f"predicate never became True within {timeout}s")


@pytest.fixture
def setup_sign_in(fake_claude, monkeypatch):
    """Write the signed-in file into FAKE_CLAUDE_HOME so the fake appears signed in."""
    home = os.environ["FAKE_CLAUDE_HOME"]
    pathlib.Path(home, "signed-in").write_text("test@example.com")


@pytest.fixture
def rc(setup_sign_in, fake_claude, tmp_path):
    """A RemoteControl instance signed in, with a clean env_file."""
    console = Console()
    env_file = tmp_path / "env"
    env_file.write_text("")
    rc = RemoteControl(
        console,
        workdir=str(tmp_path / "workdir"),
        args=[],
        env_file=env_file,
        claude=str(fake_claude),
    )
    (tmp_path / "workdir").mkdir()
    yield rc
    rc.stop()


def test_waits_while_signed_out(fake_claude, tmp_path):
    """Waits while signed out, then runs after signing in."""
    console = Console()
    env_file = tmp_path / "env"
    env_file.write_text("")
    home = os.environ["FAKE_CLAUDE_HOME"]
    # Do NOT write the signed-in file.
    rc = RemoteControl(
        console,
        workdir=str(tmp_path / "workdir"),
        args=[],
        env_file=env_file,
        claude=str(fake_claude),
    )
    (tmp_path / "workdir").mkdir()
    rc.start()

    # State should become "signed-out" and no process should start.
    def signed_out():
        return rc.status()["state"] == "signed-out"
    until(signed_out)

    # Write the signed-in file and wake the loop.
    pathlib.Path(home, "signed-in").write_text("test@example.com")
    rc._wake.set()

    # State should become "running".
    def running():
        return rc.status()["state"] == "running"
    until(running)
    rc.stop()


def test_runs_and_feeds_the_console(rc):
    """Remote Control's output appears in the console."""
    rc.start()
    until(lambda: b"Remote Control connected" in rc.console.output())
    assert b"Remote Control connected" in rc.console.output()


def test_env_file_applies_at_start(fake_claude, setup_sign_in, tmp_path):
    """The env file is merged into the process environment at every start."""
    console = Console()
    env_file = tmp_path / "env"
    env_file.write_text("FAKE_SEEN=hello\n")
    rc = RemoteControl(
        console,
        workdir=str(tmp_path / "workdir"),
        args=[],
        env_file=env_file,
        claude=str(fake_claude),
    )
    (tmp_path / "workdir").mkdir()
    rc.start()
    until(lambda: b"env FAKE_SEEN=hello" in rc.console.output())
    assert b"env FAKE_SEEN=hello" in rc.console.output()
    rc.stop()


def test_trust_waits_for_a_person(fake_claude, setup_sign_in, tmp_path):
    """Does not answer trust on its own; answer_trust() is needed."""
    console = Console()
    env_file = tmp_path / "env"
    env_file.write_text("")
    os.environ["FAKE_TRUST"] = "1"
    rc = RemoteControl(
        console,
        workdir=str(tmp_path / "workdir"),
        args=[],
        env_file=env_file,
        claude=str(fake_claude),
    )
    (tmp_path / "workdir").mkdir()
    rc.start()

    # State should become "trust-asked" and stay there for at least 1 second.
    def trust_asked():
        return rc.status()["state"] == "trust-asked"
    until(trust_asked)
    time.sleep(1)
    assert rc.status()["state"] == "trust-asked"

    # Now answer the trust question.
    # Nothing answered it: the fake only prints this once it has read a "y".
    assert b"Remote Control connected" not in rc.console.output()
    result = rc.answer_trust()
    assert result is True
    until(lambda: rc.status()["state"] == "running")
    until(lambda: b"Remote Control connected" in rc.console.output())
    del os.environ["FAKE_TRUST"]
    rc.stop()


def test_answer_trust_refused_when_not_asked(fake_claude, setup_sign_in, tmp_path):
    """answer_trust() returns False when not in trust-asked state."""
    console = Console()
    env_file = tmp_path / "env"
    env_file.write_text("")
    rc = RemoteControl(
        console,
        workdir=str(tmp_path / "workdir"),
        args=[],
        env_file=env_file,
        claude=str(fake_claude),
    )
    (tmp_path / "workdir").mkdir()
    rc.start()
    until(lambda: rc.status()["state"] == "running")
    assert rc.answer_trust() is False
    rc.stop()


def test_restarts_with_backoff(fake_claude, setup_sign_in, tmp_path):
    """Exits restart with backoff; restarts reaches 2 within 10 s."""
    os.environ["FAKE_RC_EXIT_AFTER"] = "0.2"
    console = Console()
    env_file = tmp_path / "env"
    env_file.write_text("")
    rc = RemoteControl(
        console,
        workdir=str(tmp_path / "workdir"),
        args=[],
        env_file=env_file,
        claude=str(fake_claude),
        backoff_start=0.2,
        backoff_max=0.4,
        stable_after=100,
    )
    (tmp_path / "workdir").mkdir()
    rc.start()

    # restarts should reach 2 within 10 seconds.
    def two_restarts():
        return rc.status()["restarts"] >= 2
    until(two_restarts)

    assert rc.status()["last_exit"] == 3
    # The backoff path ran: its note says how long it waits.
    output = rc.console.output().decode("utf-8", errors="replace")
    assert "starting again in 0.2s" in output

    # _delay should never exceed backoff_max.
    with rc._lock:
        assert rc._delay <= 0.4
    del os.environ["FAKE_RC_EXIT_AFTER"]
    rc.stop()


def test_stop_stays_stopped(rc):
    """After stop, no process runs and restarts doesn't grow."""
    rc.start()
    until(lambda: rc.status()["state"] == "running")
    rc.stop()
    assert rc.status()["state"] == "stopped"
    time.sleep(1)
    assert not rc._proc.running()
    restarts_after = rc.status()["restarts"]
    time.sleep(0.5)
    assert rc.status()["restarts"] == restarts_after


def test_restart_is_immediate(fake_claude, setup_sign_in, tmp_path):
    """restart() causes a new pid within 2 seconds, well under backoff_start=5."""
    console = Console()
    env_file = tmp_path / "env"
    env_file.write_text("")
    rc = RemoteControl(
        console,
        workdir=str(tmp_path / "workdir"),
        args=[],
        env_file=env_file,
        claude=str(fake_claude),
        backoff_start=5.0,
    )
    (tmp_path / "workdir").mkdir()
    rc.start()
    until(lambda: rc.status()["state"] == "running")
    old_pid = rc.status()["pid"]

    rc.restart()

    def new_pid():
        s = rc.status()
        return s["pid"] is not None and s["pid"] != old_pid
    until(new_pid, timeout=2)
    rc.stop()


def test_an_api_key_beside_the_sign_in_is_named_not_looped(rc, monkeypatch):
    """Remote Control refuses an API key, so the supervisor waits and says so once instead of restarting forever."""
    monkeypatch.setenv("FAKE_API_KEY_SOURCE", "ANTHROPIC_API_KEY")
    rc.start()
    until(lambda: b"API key is set as well (ANTHROPIC_API_KEY)" in rc.console.output())
    # Two more checks of the sign-in, which must not repeat the note.
    for _ in range(2):
        rc._wake.set()
        time.sleep(0.3)
    output = rc.console.output()
    assert rc.status()["state"] == "signed-out"
    assert output.count(b"API key is set as well") == 1
    assert b"starting Remote Control" not in output

    monkeypatch.delenv("FAKE_API_KEY_SOURCE")
    rc._wake.set()
    until(lambda: rc.status()["state"] == "running")


def test_remote_control_never_runs_as_another_account(rc, tmp_path):
    """Signed in as anyone but the box's owner, Remote Control waits and says why: it would run with this box's keys."""
    owner = tmp_path / "owner"
    owner.write_text('{"email": "owner@example.com", "orgId": "org-1"}')
    rc.owner_file = owner
    rc.start()
    until(lambda: b"isn't this box's account (owner@example.com)" in rc.console.output())
    assert rc.status()["state"] == "signed-out"
    assert b"starting Remote Control" not in rc.console.output()

    owner.write_text('{"email": "test@example.com", "orgId": "org-1"}')
    rc._wake.set()
    until(lambda: rc.status()["state"] == "running")


def test_a_stop_during_the_sign_in_check_is_kept(rc, monkeypatch):
    """Stop clicked while the loop is checking the sign-in must not be followed by a start."""
    from opsbox.manager import claude as claude_module

    real = claude_module.auth_status

    def stopped_meanwhile(**kwargs):
        status = real(**kwargs)
        rc.stop()
        return status

    monkeypatch.setattr(claude_module, "auth_status", stopped_meanwhile)
    rc.start()
    until(lambda: not rc._thread.is_alive())
    assert rc.status()["state"] == "stopped"
    assert rc.status()["pid"] is None
    assert b"starting Remote Control" not in rc.console.output()


def test_a_failed_start_backs_off_and_says_why(setup_sign_in, fake_claude, tmp_path):
    """If the process can't start (its directory is a file now), the loop says so and tries again, rather than dying."""
    blocker = tmp_path / "workdir"
    blocker.write_text("a file where the directory was")
    rc = RemoteControl(Console(), workdir=str(blocker), args=[], env_file=tmp_path / "env",
                       claude=str(fake_claude), backoff_max=0.3)
    try:
        rc.start()
        until(lambda: b"couldn't start Remote Control" in rc.console.output())
        assert rc._thread.is_alive()
        blocker.unlink()
        until(lambda: rc.status()["state"] == "running")
        assert rc.console.output().count(b"couldn't start Remote Control") == 1
    finally:
        rc.stop()


def test_restart_with_nothing_running_changes_nothing(rc):
    """Restart while stopped returns False and leaves no flag behind to skip the next crash's backoff."""
    assert rc.restart() is False
    assert rc._restart_now is False


def test_a_viewer_that_falls_behind_is_dropped():
    """A console viewer that stops reading is dropped at its limit, rather than growing without bound."""
    console = Console()
    _, q = console.attach(limit=2)
    for chunk in (b"a", b"b", b"c"):
        console.feed(chunk)
    assert console.subscribed(q) is False
    assert console.output() == b"abc"
