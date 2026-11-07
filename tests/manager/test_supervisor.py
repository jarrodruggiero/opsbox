"""Tests for the RemoteControl supervisor."""
from __future__ import annotations

import os
import pathlib
import time

import pytest

from opsbox.manager import ptyproc
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


FRAME = b"\x1b[2A\x1b[Jone\r\ntwo\r\n"


@pytest.mark.parametrize("frame", [FRAME, FRAME.replace(b"[2A", b"[35A")])
def test_the_same_repaint_again_is_dropped(frame):
    """Nothing changes on screen, and it would cost the log and the reload history every second."""
    console = Console()
    viewer = console.subscribe()
    console.feed(frame)
    console.feed(frame)
    assert console.output() == frame
    assert viewer.get_nowait() == frame
    assert viewer.empty()


def test_a_changed_repaint_is_kept():
    console = Console()
    console.feed(FRAME)
    console.feed(FRAME.replace(b"two", b"2"))
    assert console.output() == FRAME + FRAME.replace(b"two", b"2")


@pytest.mark.parametrize(
    "line",
    [
        b"ok\r\n",
        b"\x1b[1Aok\r\n",  # without the erase, it isn't known to be a redraw
        b"\x1b[1A\x1b[Jlog line\r\nstatus\r\n",  # a log line above the block: each one counts
        b"\x1b[1A\x1b[J",  # nothing drawn: each one erases further
        b"\x1b[" + b"9" * 5000 + b"A\x1b[Jhuge\r\n",  # any program can print this
    ],
)
def test_repeated_output_that_isnt_a_repaint_is_kept(line):
    """The same line twice is two lines. Only a chunk that ends where it began can go."""
    console = Console()
    console.feed(line)
    console.feed(line)
    assert console.output() == line + line


def test_a_repaint_with_a_wrapped_line_still_counts():
    """It backs up by rows, and a line that wraps is one newline over two rows."""
    frame = b"\x1b[3A\x1b[Ja line long enough to wrap\r\ntwo\r\n"
    console = Console()
    console.feed(frame)
    console.feed(frame)
    assert console.output() == frame


def test_a_console_can_keep_every_repaint():
    console = Console(collapse_repaints=False)
    console.feed(FRAME)
    console.feed(FRAME)
    assert console.output() == FRAME + FRAME


def test_a_repaint_after_something_else_is_kept():
    """Something else moved the cursor, so this repaint lands elsewhere."""
    console = Console()
    console.feed(FRAME)
    console.note("hello")
    console.feed(FRAME)
    assert console.output().count(FRAME) == 2


def test_a_changing_repaint_reaches_the_console_in_one_piece(rc, monkeypatch):
    """Erased and redrawn in the same chunk, so a page can't draw the block blank."""
    # A window a stall on a busy machine can't close, and still shorter than the period.
    monkeypatch.setenv("FAKE_RC_REPAINT", "0.5")
    monkeypatch.setenv("FAKE_RC_REPAINT_CHANGES", "1")
    monkeypatch.setattr(ptyproc, "BURST_QUIET", 0.2)
    monkeypatch.setattr(ptyproc, "BURST_LIMIT", 1.0)
    viewer = rc.console.subscribe()
    rc.start()
    until(lambda: rc.console.output().count(b"\x1b[J") >= 4)
    chunks = []
    while not viewer.empty():
        chunks.append(viewer.get_nowait())
    erasing = [chunk for chunk in chunks if b"\x1b[J" in chunk]
    assert len(erasing) >= 3
    assert all(b"space to show QR code" in chunk for chunk in erasing), erasing[:3]


def test_an_unchanged_repaint_costs_the_console_nothing(rc, monkeypatch):
    """With a session attached the real CLI repaints every second, and this is what stays quiet."""
    monkeypatch.setenv("FAKE_RC_REPAINT", "0.1")
    monkeypatch.setattr(ptyproc, "BURST_QUIET", 0.04)
    monkeypatch.setattr(ptyproc, "BURST_LIMIT", 1.0)
    rc.start()
    until(lambda: b"space to show QR code" in rc.console.output())
    time.sleep(2)
    # Twice when all goes well: the first drawing has nothing to erase, so it
    # differs from the second. Room for a stall, far short of the twenty it drew.
    assert rc.console.output().count(b"space to show QR code") <= 8


def test_stop_is_noted_after_the_process_s_last_words(rc, monkeypatch):
    """They move relative to the cursor, so a note before them would put them in the wrong place."""
    monkeypatch.setenv("FAKE_RC_REPAINT", "5")
    rc.start()
    until(lambda: b"space to show QR code" in rc.console.output())
    rc.stop()
    until(lambda: b"[opsbox] stopped" in rc.console.output())
    output = rc.console.output()
    assert output.index(b"Environment preserved") < output.index(b"[opsbox] stopped")


def test_restart_is_noted_after_the_process_s_last_words(rc, monkeypatch):
    monkeypatch.setenv("FAKE_RC_REPAINT", "5")
    rc.start()
    until(lambda: b"space to show QR code" in rc.console.output())
    assert rc.restart()
    until(lambda: b"[opsbox] restarting" in rc.console.output())
    output = rc.console.output()
    assert output.index(b"Environment preserved") < output.index(b"[opsbox] restarting")


def test_stop_is_noted_with_a_job_left_holding_the_terminal(rc, monkeypatch):
    """On Linux the stream doesn't end while the job lives, though the process has gone."""
    monkeypatch.setenv("FAKE_RC_LEAVE_BEHIND", "20")
    rc.start()
    until(lambda: b"Remote Control connected" in rc.console.output())
    rc.stop()
    until(lambda: b"[opsbox] stopped" in rc.console.output(), timeout=8)


def test_stop_cancels_a_restart_still_under_way(rc, monkeypatch):
    """Or the next run's first crash would count as that restart, and skip its wait."""
    monkeypatch.setenv("FAKE_RC_REPAINT", "5")
    rc.start()
    until(lambda: b"space to show QR code" in rc.console.output())
    # What a restart leaves while its process is ending, which is when a Stop can
    # come: restart() itself doesn't return until the process has gone.
    rc._restart_now = True
    rc.stop()
    until(lambda: b"[opsbox] stopped" in rc.console.output())
    monkeypatch.delenv("FAKE_RC_REPAINT")
    monkeypatch.setenv("FAKE_RC_EXIT_AFTER", "0")
    rc.start()
    until(lambda: rc.status()["state"] == "backing-off")
    assert b"[opsbox] restarting" not in rc.console.output()


def test_restart_during_a_wait_is_noted_at_once(rc, monkeypatch):
    """No process to wait for here, and the wait itself may be long."""
    monkeypatch.setenv("FAKE_RC_EXIT_AFTER", "0")
    rc.start()
    until(lambda: rc.status()["state"] == "backing-off")
    monkeypatch.delenv("FAKE_RC_EXIT_AFTER")
    assert rc.restart()
    assert b"[opsbox] restarting" in rc.console.output()


def test_stop_with_nothing_running_is_noted_once(fake_claude, tmp_path):
    """Signed out there is no process to wait for, and it still says so."""
    env_file = tmp_path / "env"
    env_file.write_text("")
    rc = RemoteControl(Console(), workdir=str(tmp_path), args=[], env_file=env_file, claude=str(fake_claude))
    rc.start()
    until(lambda: rc.status()["state"] == "signed-out")
    rc.stop()
    until(lambda: b"[opsbox] stopped" in rc.console.output())
    time.sleep(0.2)
    assert rc.console.output().count(b"[opsbox] stopped") == 1


def test_the_skills_library_is_brought_up_to_date_before_each_start(rc, tmp_path, monkeypatch):
    """And what happened is said in the console, ahead of Remote Control's own output."""
    monkeypatch.setenv("HOME", str(tmp_path / "box-home"))
    (tmp_path / "box-home").mkdir()
    rc.env_file.write_text(f"OPSBOX_SKILLS_REPO={tmp_path / 'missing.git'}\n")
    rc.start()
    until(lambda: b"Remote Control connected" in rc.console.output())
    output = rc.console.output()
    assert output.index(b"[opsbox] skills: couldn't") < output.index(b"[opsbox] starting Remote Control")


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
