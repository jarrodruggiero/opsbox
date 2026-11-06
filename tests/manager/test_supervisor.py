"""Tests for the RemoteControl supervisor."""
from __future__ import annotations

import os
import pathlib
import time

import pytest

from opsbox.manager.supervisor import MIN_COLS, MIN_ROWS, Console, RemoteControl, pending_question


TRUST = "Trust /home/opsbox/workspace? [y/N]"
ENABLE = "Enable Remote Control? (y/n)"


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


def test_a_question_waits_for_a_person(rc, monkeypatch):
    """The manager never answers on its own: the fake only prints "connected" once it has read a "y"."""
    monkeypatch.setenv("FAKE_TRUST", "1")
    rc.start()
    until(lambda: rc.status()["state"] == "asking")
    time.sleep(1)
    assert rc.status()["state"] == "asking"
    assert rc.status()["question"] == "Trust /home/opsbox/workspace? [y/N]"
    assert b"Remote Control connected" not in rc.console.output()

    assert rc.answer(True, TRUST) is True
    until(lambda: b"Remote Control connected" in rc.console.output())
    assert rc.status()["state"] == "running" and rc.status()["question"] is None


def test_both_first_start_questions_are_asked_and_answered(rc, monkeypatch):
    """The real CLI asks to trust the directory, then for one-time consent: both must reach the page, in turn."""
    monkeypatch.setenv("FAKE_TRUST", "1")
    monkeypatch.setenv("FAKE_ENABLE", "1")
    rc.start()
    until(lambda: rc.status()["question"] == TRUST)
    assert rc.answer(True, TRUST) is True
    until(lambda: rc.status()["question"] == ENABLE)
    assert rc.status()["state"] == "asking"
    assert rc.answer(True, ENABLE) is True
    until(lambda: b"Remote Control connected" in rc.console.output())
    assert rc.status()["state"] == "running"

    # Both answers were saved, so the next start asks nothing.
    assert rc.restart() is True
    until(lambda: rc.console.output().count(b"Remote Control connected") == 2)
    assert rc.status()["state"] == "running"


def test_an_answer_only_counts_for_the_question_it_was_read_as(rc, monkeypatch):
    """A double-click on Trust must not also answer the next question, which nobody has read."""
    monkeypatch.setenv("FAKE_TRUST", "1")
    monkeypatch.setenv("FAKE_ENABLE", "1")
    rc.start()
    until(lambda: rc.status()["question"] == TRUST)
    assert rc.answer(True, TRUST) is True
    until(lambda: rc.status()["question"] == ENABLE)
    assert rc.answer(True, TRUST) is False
    time.sleep(0.3)
    assert rc.status()["question"] == ENABLE
    assert b"Remote Control connected" not in rc.console.output()


def test_a_stop_while_signed_out_leaves_it_startable(fake_claude, tmp_path, monkeypatch):
    """Stopped during the sign-in check, it says stopped (so Start shows), rather than signed-out with nothing running."""
    from opsbox.manager import claude as claude_module

    env_file = tmp_path / "env"
    rc = RemoteControl(Console(), workdir=str(tmp_path / "workdir"), args=[], env_file=env_file, claude=str(fake_claude))
    real = claude_module.auth_status

    def stopped_meanwhile(**kwargs):
        status = real(**kwargs)
        rc.stop()
        return status

    monkeypatch.setattr(claude_module, "auth_status", stopped_meanwhile)
    rc.start()
    until(lambda: not rc._thread.is_alive())
    assert rc.status()["state"] == "stopped"


def test_typing_the_answer_works_too(rc, monkeypatch):
    """The console is a real terminal: a "y" typed there answers, and the page stops asking."""
    monkeypatch.setenv("FAKE_ENABLE", "1")
    rc.start()
    until(lambda: rc.status()["state"] == "asking")
    assert rc.write(b"y\r") is True
    until(lambda: b"Remote Control connected" in rc.console.output())
    until(lambda: rc.status()["state"] == "running")
    assert rc.status()["question"] is None


def test_no_stops_rather_than_asking_again(rc, monkeypatch):
    """No ends the process, and restarting it would only ask again, so the loop stops too."""
    monkeypatch.setenv("FAKE_ENABLE", "1")
    rc.start()
    until(lambda: rc.status()["state"] == "asking")
    assert rc.answer(False, ENABLE) is True
    until(lambda: not rc._thread.is_alive())
    assert rc.status()["state"] == "stopped"
    assert b"Remote Control connected" not in rc.console.output()
    assert rc.console.output().count(b"Enable Remote Control?") == 1


def test_answer_is_refused_when_nothing_is_asked(rc):
    """A click left over from a question already gone must not type into the terminal."""
    rc.start()
    until(lambda: rc.status()["state"] == "running")
    assert rc.answer(True, ENABLE) is False


@pytest.mark.parametrize("output, question", [
    ("Trust /home/opsbox/workspace? [y/N] ", "Trust /home/opsbox/workspace? [y/N]"),
    ("before\r\nEnable Remote Control? (y/n) ", "Enable Remote Control? (y/n)"),
    ("\x1b[33mSecurity warnings found.\x1b[0m Continue anyway? [y/N] ", "Security warnings found. Continue anyway? [y/N]"),
    ("Enable Remote Control? (y/n) y\r\n", None),
    ("Enable Remote Control? (y/n) \r\nconnected\r\n", None),
    ("the docs say answer (y/n) somewhere\r\nand then more\r\n", None),
    ("", None),
])
def test_only_a_question_at_the_end_counts(output, question):
    """A question is what the output ends on, without a trailing space to trip it, and nothing older."""
    assert pending_question(output) == question


def test_the_terminal_never_shrinks_below_what_the_cli_needs(rc):
    """The trust check refuses to ask in a tiny terminal, so a phone's size is only taken above the floor."""
    rc.resize(40, 10)
    rc.start()
    until(lambda: rc.status()["state"] == "running")
    size = rc._proc._cols, rc._proc._rows
    assert size == (MIN_COLS, MIN_ROWS)
    rc.resize(150, 50)
    assert (rc._cols, rc._rows) == (150, 50)


def test_write_with_nothing_running_says_so(rc):
    """Keystrokes with no process behind them are refused, not lost in silence."""
    assert rc.write(b"x") is False


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


def test_remote_control_runs_in_a_terminal_that_does_colour(rc, monkeypatch):
    """A container has no TERM of its own, and the page's terminal does colour."""
    monkeypatch.delenv("TERM", raising=False)
    rc.start()
    until(lambda: b"env TERM=xterm-256color" in rc.console.output())
