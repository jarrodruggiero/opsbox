"""Tests for the shell on the box, with a real /bin/sh."""
from __future__ import annotations

import pathlib
import time

import pytest

from opsbox.manager.keys import Refused
from opsbox.manager.shell import Shells


def until(predicate, timeout=10):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.02)
    raise AssertionError(f"never became true within {timeout}s")


@pytest.fixture
def shells(tmp_path: pathlib.Path):
    """Shells that run /bin/sh in a scratch directory, with one setting in the env file."""
    env_file = tmp_path / "env"
    env_file.write_text("FROM_THE_PAGE=yes\n")
    opened = Shells(workdir=str(tmp_path / "work"), env_file=env_file, argv=["/bin/sh"], limit=2)
    yield opened
    opened.close_all()


def test_typing_reaches_the_shell_and_output_comes_back(shells: Shells) -> None:
    shell = shells.open("me", 80, 24)
    assert shell.write(b"echo hello-$((40+2))\r") is True
    until(lambda: b"hello-42" in shell.console.output())


def test_it_starts_in_the_workspace_with_the_pages_settings(shells: Shells, tmp_path: pathlib.Path) -> None:
    """A shell sees what Remote Control sees, in the directory sessions start in, in a terminal that does colour."""
    shell = shells.open("me", 80, 24)
    shell.write(b'echo "[$PWD|$FROM_THE_PAGE|$TERM]"\r')
    until(lambda: b"work|yes|xterm-256color]" in shell.console.output())
    assert (tmp_path / "work").is_dir()


def test_it_opens_at_the_browsers_size_and_follows_it(shells: Shells) -> None:
    shell = shells.open("me", 100, 30)
    shell.write(b"stty size\r")
    until(lambda: b"30 100" in shell.console.output())
    shell.resize(120, 40)
    shell.write(b"stty size\r")
    until(lambda: b"40 120" in shell.console.output())


def test_a_browser_keeps_its_shell_across_a_reload(shells: Shells) -> None:
    """Opening again returns the same shell, so a reload doesn't lose what's running."""
    first = shells.open("me", 80, 24)
    assert shells.open("me", 80, 24) is first
    assert shells.get("me") is first


def test_each_browser_gets_its_own(shells: Shells) -> None:
    assert shells.open("me", 80, 24) is not shells.open("you", 80, 24)
    assert shells.get("nobody") is None


def test_a_limit_keeps_a_browser_from_filling_the_box(shells: Shells) -> None:
    shells.open("one", 80, 24)
    shells.open("two", 80, 24)
    with pytest.raises(Refused, match="Too many shells"):
        shells.open("three", 80, 24)
    shells.close("one")
    assert shells.open("three", 80, 24).running()


def test_after_exit_the_stream_ends_and_opening_starts_a_new_one(shells: Shells) -> None:
    """exit ends the viewer's stream (so the page can say so), and the last output still shows."""
    shell = shells.open("me", 80, 24)
    shell.write(b"echo bye; exit\r")
    until(lambda: shell.console.closed)
    backlog, q = shell.console.attach()
    assert b"bye" in backlog
    assert q.get(timeout=1) is None
    again = shells.open("me", 80, 24)
    assert again is not shell and again.running()


def test_closing_ends_a_shell_that_ignores_terminate(shells: Shells) -> None:
    """Signing a browser out must end its shell promptly, and an interactive one ignores SIGTERM."""
    shell = shells.open("me", 80, 24)
    shell.write(b"trap '' TERM; echo ready\r")
    until(lambda: b"ready" in shell.console.output().replace(b"echo ready", b""))
    started = time.monotonic()
    shells.close("me")
    assert not shell.running()
    assert time.monotonic() - started < 4
    assert shells.get("me") is None


def test_close_all_ends_every_shell(shells: Shells) -> None:
    first, second = shells.open("one", 80, 24), shells.open("two", 80, 24)
    shells.close_all()
    assert not first.running() and not second.running()


def test_a_shell_nobody_is_viewing_is_closed_after_the_limit(tmp_path: pathlib.Path) -> None:
    """Not while someone is viewing it, however long; only once they've gone."""
    shells = Shells(workdir=str(tmp_path / "work"), env_file=tmp_path / "env", argv=["/bin/sh"], idle_seconds=0.2)
    try:
        shell = shells.open("me", 80, 24)
        shell.viewer_joined()
        time.sleep(0.4)
        shells.reap()
        assert shell.running()

        shell.viewer_left()
        time.sleep(0.4)
        shells.reap()
        assert not shell.running()
        assert shells.get("me") is None
    finally:
        shells.close_all()


def test_a_shell_running_something_isnt_closed_however_long_it_goes_unviewed(tmp_path: pathlib.Path) -> None:
    """A long terraform apply must survive the tab being on something else, and is closed once it has finished."""
    shells = Shells(workdir=str(tmp_path / "work"), env_file=tmp_path / "env", argv=["/bin/bash", "--norc", "--noprofile"], idle_seconds=0.1)
    try:
        shell = shells.open("me", 80, 24)
        shell.write(b"sleep 30\r")
        until(shell.busy)
        time.sleep(0.3)
        shells.reap()
        assert shell.running(), "closed with a command still running"

        shell.write(b"\x03")
        until(lambda: not shell.busy())
        time.sleep(0.3)
        shells.reap()
        assert not shell.running()
    finally:
        shells.close_all()


def test_a_directory_that_cant_be_made_is_a_refusal(tmp_path: pathlib.Path) -> None:
    (tmp_path / "file").write_text("in the way")
    shells = Shells(workdir=str(tmp_path / "file/work"), env_file=tmp_path / "env", argv=["/bin/sh"])
    with pytest.raises(Refused, match="couldn't start"):
        shells.open("me", 80, 24)


def test_a_shell_that_cant_start_is_a_refusal(tmp_path: pathlib.Path) -> None:
    shells = Shells(workdir=str(tmp_path), env_file=tmp_path / "env", argv=["/nonexistent/shell"])
    with pytest.raises(Refused, match="couldn't start"):
        shells.open("me", 80, 24)
    assert shells.get("me") is None
