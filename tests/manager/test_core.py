"""Tests for ptyproc, claude, and the fake_claude fixture."""
from __future__ import annotations

import os
import pathlib
import sys
import time

import pytest

from opsbox.manager import claude, ptyproc


class TestPtyProcess:
    """Tests for PtyProcess."""

    def test_pty_runs_and_keeps_output(self) -> None:
        """Runs a program and keeps its output."""
        proc = ptyproc.PtyProcess(["sh", "-c", "printf hello; printf world"])
        proc.start()
        exit_code = proc.wait(5)
        assert exit_code == 0
        out = proc.output()
        cleaned = out.replace(b"\r\n", b"").replace(b"\r", b"")
        assert b"helloworld" in cleaned

    def test_pty_streams_to_subscribers(self) -> None:
        """Subscribes before start and collects streamed chunks."""
        proc = ptyproc.PtyProcess(["sh", "-c", "printf one; sleep 0.2; printf two"])
        q = proc.subscribe()
        proc.start()
        chunks: list[bytes] = []
        while True:
            item = q.get()
            if item is None:
                break
            chunks.append(item)
        joined = b"".join(chunks)
        assert b"one" in joined
        assert b"two" in joined

    def test_pty_is_a_terminal(self) -> None:
        """The process sees a real terminal on stdin and stdout."""
        proc = ptyproc.PtyProcess(["sh", "-c", "test -t 0 && test -t 1 && printf tty"])
        proc.start()
        exit_code = proc.wait(5)
        assert exit_code == 0
        out = proc.output()
        assert b"tty" in out

    def test_pty_keeps_only_the_last_bytes(self) -> None:
        """Buffers are trimmed to keep_bytes from the front."""
        proc = ptyproc.PtyProcess(
            ["sh", "-c", "for i in $(seq 1 5000); do printf x; done; printf END"],
            keep_bytes=1000,
        )
        proc.start()
        exit_code = proc.wait(10)
        assert exit_code == 0
        out = proc.output()
        assert len(out) <= 1000
        assert out.endswith(b"END")

    def test_terminate_stops_the_process_group(self) -> None:
        """Terminates the process group within the grace period."""
        proc = ptyproc.PtyProcess(["sh", "-c", "sleep 30"])
        proc.start()
        proc.terminate(grace=1)
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline:
            if not proc.running():
                break
            time.sleep(0.1)
        assert not proc.running()


class TestClaude:
    """Tests for claude module functions."""

    def test_find_login_url_prefers_osc8(self) -> None:
        """OSC 8 hyperlinks are preferred over plain text."""
        osc8 = (
            "If the browser didn't open, visit: \x1b]8;;https://claude.com/cai/"
            "oauth/authorize?code=true&client_id=fake&state=abc\x1b\\"
            "https://claude.com/cai/oauth/authorize?code=true&client_id=fake&state=abc"
            "\x1b]8;;\x1b\\"
        )
        assert claude.find_login_url(osc8) == "https://claude.com/cai/oauth/authorize?code=true&client_id=fake&state=abc"

        plain = "Visit https://example.com/oauth/authorize?token=abc to continue."
        assert claude.find_login_url(plain) == "https://example.com/oauth/authorize?token=abc"

        assert claude.find_login_url("no link here") is None

    def test_identity_needs_a_claude_ai_sign_in(self) -> None:
        """Identity requires loggedIn=True, authMethod=claude.ai, and non-empty values."""
        assert claude.identity({"loggedIn": True, "authMethod": "api_key", "email": "x", "orgId": "y"}) is None
        assert claude.identity({"loggedIn": False}) is None

    @pytest.mark.parametrize("output", [None, "garbage", "[1, 2]"], ids=["missing", "garbage", "not-an-object"])
    def test_a_failed_status_is_an_error_not_signed_out(self, output, monkeypatch: pytest.MonkeyPatch, tmp_path: pathlib.Path) -> None:
        """A status that couldn't be read says so; deciding "signed out" from it would let a sign-in run over the box's own."""
        if output is None:
            monkeypatch.setattr(claude, "CLAUDE", str(tmp_path / "missing"))
        else:
            fake = tmp_path / "claude"
            fake.write_text(f"#!/bin/sh\necho '{output}'\n")
            fake.chmod(0o755)
            monkeypatch.setattr(claude, "CLAUDE", str(fake))
        result = claude.auth_status()
        assert result["loggedIn"] is False and result["error"]
        assert claude.identity(result) is None
        assert claude.problem(result).startswith("couldn't check the sign-in")


class TestLoginFlow:
    """Tests for LoginFlow with the fake_claude fixture."""

    def test_login_flow_relays_the_code(self, fake_claude: pathlib.Path, tmp_path: pathlib.Path) -> None:
        """Full sign-in flow: start, get URL, submit code, verify identity."""
        cfg = tmp_path / "cfg"
        cfg.mkdir()
        flow = claude.LoginFlow(config_dir=str(cfg))
        flow.start()
        url = flow.url()
        assert url is not None
        assert url.startswith("https://claude.com/cai/oauth/authorize")
        flow.submit("good-code-123")
        assert flow.wait(10) is True
        status = claude.auth_status(config_dir=str(cfg))
        assert status["loggedIn"] is True
        assert claude.identity(status) == ("me@example.com", "org-1")

    def test_login_flow_wrong_code_fails(self, fake_claude: pathlib.Path, tmp_path: pathlib.Path) -> None:
        """Submitting a wrong code causes wait to return False."""
        cfg = tmp_path / "cfg"
        cfg.mkdir()
        flow = claude.LoginFlow(config_dir=str(cfg))
        flow.start()
        flow.url()
        flow.submit("wrong-code-999")
        assert flow.wait(10) is False

    def test_submit_refuses_junk(self, fake_claude: pathlib.Path, tmp_path: pathlib.Path) -> None:
        """Submit rejects codes that don't match the CODE pattern."""
        cfg = tmp_path / "cfg"
        cfg.mkdir()
        flow = claude.LoginFlow(config_dir=str(cfg))
        flow.start()
        with pytest.raises(ValueError):
            flow.submit("not a code; rm -rf /")
        with pytest.raises(ValueError):
            flow.submit("")
        flow.cancel()

    def test_config_dirs_are_separate(self, fake_claude: pathlib.Path, tmp_path: pathlib.Path) -> None:
        """Sign-in in one config dir does not affect another."""
        cfg_a = tmp_path / "cfg_a"
        cfg_b = tmp_path / "cfg_b"
        cfg_a.mkdir()
        cfg_b.mkdir()

        flow = claude.LoginFlow(config_dir=str(cfg_a))
        flow.start()
        flow.url()
        flow.submit("good-code-123")
        assert flow.wait(10) is True

        # Config dir B should not see the sign-in.
        status = claude.auth_status(config_dir=str(cfg_b))
        assert status["loggedIn"] is False


def test_problem_names_what_stops_remote_control() -> None:
    """Only a claude.ai sign-in with no API key beside it can start Remote Control; anything else says why."""
    ok = {"loggedIn": True, "authMethod": "claude.ai", "email": "me@example.com", "orgId": "org-1", "apiKeySource": "none"}
    assert claude.problem(ok) is None
    assert claude.problem({k: v for k, v in ok.items() if k != "apiKeySource"}) is None
    assert "not signed in" in claude.problem({"loggedIn": False, "authMethod": "none"})
    assert "(ANTHROPIC_API_KEY)" in claude.problem({**ok, "apiKeySource": "ANTHROPIC_API_KEY"})
    assert "signed in with api_key" in claude.problem({"loggedIn": True, "authMethod": "api_key"})


def test_a_burst_of_small_writes_arrives_as_one_chunk(monkeypatch: pytest.MonkeyPatch) -> None:
    """A terminal program's one repaint is many writes, with the screen blank between two of them.

    One message per write let the page draw that blank state: with a session
    attached, Claude's status block flashed every second.
    """
    # Gaps a reader can't miss, or the pieces arrive together with no gathering at
    # all, and a window that can't close on a busy machine.
    monkeypatch.setattr(ptyproc, "BURST_QUIET", 0.5)
    monkeypatch.setattr(ptyproc, "BURST_LIMIT", 2.0)
    code = (
        "import os,time\nfor piece in (b'up', b'erase', b'one', b'two'):\n"
        "    os.write(1, piece)\n    time.sleep(0.02)\ntime.sleep(30)"
    )
    proc = ptyproc.PtyProcess([sys.executable, "-c", code])
    stream = proc.subscribe()
    proc.start()
    try:
        assert stream.get(timeout=10) == b"uperaseonetwo"
    finally:
        proc.terminate()


def test_the_burst_window_is_short_but_there() -> None:
    """Zero gathers nothing. Much longer and typing starts to feel late."""
    assert 0 < ptyproc.BURST_QUIET < ptyproc.BURST_LIMIT <= 0.1


def test_steady_output_isnt_held_back_until_it_stops() -> None:
    """Output with no quiet gap in it still arrives as it goes, not all at the end."""
    code = "import os,time\nfor _ in range(200):\n    os.write(1, b'x')\n    time.sleep(0.005)\ntime.sleep(30)"
    proc = ptyproc.PtyProcess([sys.executable, "-c", code])
    stream = proc.subscribe()
    proc.start()
    try:
        chunks = []
        while sum(map(len, chunks)) < 200:
            chunks.append(stream.get(timeout=10))
        assert len(chunks) >= 5, f"{len(chunks)} chunks"
    finally:
        proc.terminate()


def test_a_flood_of_output_stays_in_small_chunks() -> None:
    """A big `cat` must not gather into megabytes for a browser to take in one piece."""
    code = "import os,time\nfor _ in range(64):\n    os.write(1, b'x' * 65536)\ntime.sleep(30)"
    proc = ptyproc.PtyProcess([sys.executable, "-c", code])
    stream = proc.subscribe()
    proc.start()
    try:
        chunks = []
        while sum(map(len, chunks)) < 64 * 65536:
            chunks.append(stream.get(timeout=10))
        assert max(map(len, chunks)) < 2 * ptyproc.BURST_BYTES
    finally:
        proc.terminate()


def test_a_failed_start_leaks_no_descriptors(tmp_path: pathlib.Path) -> None:
    """A start that fails (no such directory) closes the pty pair it opened; a supervisor retrying it must not run out of fds."""
    import os

    def open_fds() -> int:
        return len(os.listdir("/dev/fd"))

    before = open_fds()
    for _ in range(20):
        proc = ptyproc.PtyProcess(["/bin/sh", "-c", "true"], env={}, cwd=str(tmp_path / "missing"))
        with pytest.raises(OSError):
            proc.start()
    assert open_fds() <= before + 1


def _open_fds() -> int:
    return len(os.listdir("/dev/fd"))


def test_a_finished_process_leaves_no_descriptors() -> None:
    """The writer thread closes the terminal once the process has gone, or a supervisor retrying runs out of fds."""
    before = _open_fds()
    for _ in range(10):
        proc = ptyproc.PtyProcess(["/bin/sh", "-c", "true"])
        proc.start()
        assert proc.wait(5) == 0
    deadline = time.monotonic() + 5
    while _open_fds() > before + 1 and time.monotonic() < deadline:
        time.sleep(0.05)
    assert _open_fds() <= before + 1


def test_input_reaches_the_process_in_order() -> None:
    """Keystrokes go through a queue, and must come out as they went in."""
    proc = ptyproc.PtyProcess(["/bin/sh", "-c", "stty -echo; read line; echo got:$line"])
    proc.start()
    for part in (b"a", b"b", b"c", b"\r"):
        proc.write(part)
    assert proc.wait(5) == 0
    deadline = time.monotonic() + 5
    while b"got:abc" not in proc.output() and time.monotonic() < deadline:
        time.sleep(0.05)
    assert b"got:abc" in proc.output()


def test_a_process_not_reading_cant_hold_a_request() -> None:
    """Writing to a process that isn't reading returns at once, then refuses, rather than blocking a web request."""
    proc = ptyproc.PtyProcess(["/bin/sh", "-c", "sleep 30"])
    proc.start()
    try:
        started = time.monotonic()
        with pytest.raises(OSError, match="isn't reading"):
            for _ in range(10):
                proc.write(b"x" * 20000)
        assert time.monotonic() - started < 2
    finally:
        proc.terminate(grace=1)


def test_resize_reaches_the_process() -> None:
    """The terminal on the page and the one the process sees stay the same size."""
    proc = ptyproc.PtyProcess(["/bin/sh", "-c", "read line; stty size"], cols=80, rows=24)
    proc.start()
    proc.resize(133, 37)
    proc.write(b"\r")
    assert proc.wait(5) == 0
    deadline = time.monotonic() + 5
    while b"37 133" not in proc.output() and time.monotonic() < deadline:
        time.sleep(0.05)
    assert b"37 133" in proc.output()


def test_hang_up_ends_what_ignores_a_terminate() -> None:
    """An interactive shell ignores SIGTERM, and closing it must not take the 5 seconds of a kill."""
    proc = ptyproc.PtyProcess(["/bin/sh", "-c", "trap '' TERM; while :; do sleep 1; done"])
    proc.start()
    time.sleep(0.2)
    started = time.monotonic()
    proc.hang_up()
    assert not proc.running()
    assert time.monotonic() - started < 4


def _run_python(script: str) -> ptyproc.PtyProcess:
    """A program that, unlike bash, doesn't take the terminal for itself, as Remote Control doesn't."""
    proc = ptyproc.PtyProcess([sys.executable, "-c", script])
    proc.start()
    return proc


def _output_has(proc: ptyproc.PtyProcess, text: bytes, timeout: float = 8) -> None:
    deadline = time.monotonic() + timeout
    while text not in proc.output():
        assert time.monotonic() < deadline, f"{text!r} never appeared in {proc.output()!r}"
        time.sleep(0.02)


def test_a_resize_signals_a_program_that_didnt_take_the_terminal() -> None:
    """stty would read the new size either way; only a controlling terminal gets the program told."""
    proc = _run_python("import signal, time\nsignal.signal(signal.SIGWINCH, lambda *a: print('told', flush=True))\nprint('ready', flush=True)\ntime.sleep(20)")
    try:
        _output_has(proc, b"ready")
        proc.resize(133, 37)
        _output_has(proc, b"told")
    finally:
        proc.terminate(grace=1)


def test_ctrl_c_reaches_a_program_that_didnt_take_the_terminal() -> None:
    """The console is meant to be real: Ctrl-C has to be an interrupt, not a stray byte."""
    proc = _run_python("import signal, time\nsignal.signal(signal.SIGINT, lambda *a: print('interrupted', flush=True))\nprint('ready', flush=True)\ntime.sleep(20)")
    try:
        _output_has(proc, b"ready")
        proc.write(b"\x03")
        _output_has(proc, b"interrupted")
    finally:
        proc.terminate(grace=1)


def test_settings_for_python_cant_stop_a_program_starting() -> None:
    """The shim that takes the terminal is Python, and a setting like PYTHONHOME is the program's business, not the shim's."""
    proc = ptyproc.PtyProcess(["/bin/sh", "-c", "echo started"], env={"PYTHONHOME": "/nonexistent", "PYTHONPATH": "/nonexistent"})
    proc.start()
    assert proc.wait(5) == 0
    _output_has(proc, b"started")


def test_a_missing_program_is_an_error_at_start_not_an_exit_later() -> None:
    """The shim would otherwise hide it behind a successful start."""
    with pytest.raises(OSError):
        ptyproc.PtyProcess(["/nonexistent/program"]).start()


def test_the_end_is_noticed_even_with_something_holding_the_terminal() -> None:
    """A job left behind keeps the terminal open for hours; the process it came from has still gone."""
    proc = ptyproc.PtyProcess(["/bin/sh", "-c", "sleep 30 & echo left; exit 0"])
    output = proc.subscribe()
    started = time.monotonic()
    proc.start()
    while output.get(timeout=10) is not None:
        pass
    assert time.monotonic() - started < 8
    assert not proc.running()


def test_busy_means_a_command_is_running() -> None:
    """What stops an unwatched shell being closed underneath a long terraform apply."""
    proc = ptyproc.PtyProcess(["/bin/bash", "--norc", "--noprofile"])
    proc.start()
    try:
        deadline = time.monotonic() + 8
        while proc.busy() and time.monotonic() < deadline:
            time.sleep(0.02)
        assert proc.busy() is False
        proc.write(b"sleep 30\r")
        while not proc.busy() and time.monotonic() < deadline:
            time.sleep(0.02)
        assert proc.busy() is True
        proc.write(b"\x03")
        while proc.busy() and time.monotonic() < deadline:
            time.sleep(0.02)
        assert proc.busy() is False
    finally:
        proc.hang_up()
