"""Tests for ptyproc, claude, and the fake_claude fixture."""
from __future__ import annotations

import os
import pathlib
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

    def test_auth_status_survives_garbage(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """When the claude binary fails, auth_status returns a safe default."""
        monkeypatch.setattr(claude, "CLAUDE", "/bin/false")
        result = claude.auth_status()
        assert result == {"loggedIn": False, "authMethod": "none"}


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
