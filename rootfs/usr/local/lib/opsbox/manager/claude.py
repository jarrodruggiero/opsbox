"""Drives the Claude Code CLI: auth status, account identity, and a sign-in flow."""
from __future__ import annotations

import json
import os
import re
import subprocess
import time
from pathlib import Path

from .ptyproc import PtyProcess

CLAUDE = "/usr/bin/claude"

ANSI = re.compile(r"\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)|\x1b\[[0-9;?]*[ -/]*[@-~]|\x1b[@-Z\\-_]")

# OSC 8 hyperlinks carry the full URL even when the visible text wraps.
OSC8_LINK = re.compile(r"\x1b\]8;[^;]*;(https://[^\x07\x1b]+)(?:\x07|\x1b\\)")

LOGIN_URL = re.compile(r"https://\S+/oauth/authorize\?\S+")

CODE = re.compile(r"[A-Za-z0-9_#.~-]{8,512}")


def strip_ansi(text: str) -> str:
    """Remove ANSI escape sequences from text."""
    return ANSI.sub("", text)


def find_login_url(output: str) -> str | None:
    """Find the OAuth authorize URL, preferring OSC 8 hyperlinks.

    The OSC 8 hyperlink carries the whole URL even when the visible text wraps.
    """
    m = OSC8_LINK.search(output)
    if m:
        url = m.group(1)
        if "/oauth/authorize" in url:
            return url
    cleaned = strip_ansi(output)
    m = LOGIN_URL.search(cleaned)
    if m:
        return m.group(0)
    return None


def _env(config_dir: str | None) -> dict[str, str]:
    """Return a copy of os.environ, setting CLAUDE_CONFIG_DIR when config_dir is given."""
    env = dict(os.environ)
    if config_dir is not None:
        env["CLAUDE_CONFIG_DIR"] = config_dir
    return env


def auth_status(config_dir: str | None = None, claude: str | None = None) -> dict:
    """Run `claude auth status` and return the parsed JSON output."""
    # A failure is never an answer: a missing binary, a hang or garbage gives a
    # status with "error" set, which identity() reads as no account and
    # problem() names. Callers that decide something from "signed out", such
    # as whose sign-in a new one becomes, must refuse on it instead.
    try:
        result = subprocess.run(
            [claude or CLAUDE, "auth", "status"],
            env=_env(config_dir),
            capture_output=True,
            text=True,
            timeout=30,
        )
        status = json.loads(result.stdout)
    except subprocess.TimeoutExpired:
        return {"loggedIn": False, "authMethod": "none", "error": "claude auth status took too long"}
    except (OSError, subprocess.SubprocessError) as failed:
        return {"loggedIn": False, "authMethod": "none", "error": f"claude auth status failed: {failed.__class__.__name__}"}
    except ValueError:
        return {"loggedIn": False, "authMethod": "none", "error": "claude auth status printed something unexpected"}
    if not isinstance(status, dict):
        return {"loggedIn": False, "authMethod": "none", "error": "claude auth status printed something unexpected"}
    status.pop("error", None)
    return status


def identity(status: dict) -> tuple[str, str] | None:
    """Return (email, orgId) when signed in via claude.ai, else None.

    email and orgId are what `claude auth status` reports for a claude.ai sign-in;
    together they say which account and which organisation.
    """
    if (
        status.get("loggedIn") is True
        and status.get("authMethod") == "claude.ai"
        and isinstance(status.get("email"), str)
        and isinstance(status.get("orgId"), str)
        and status["email"]
        and status["orgId"]
    ):
        return (status["email"], status["orgId"])
    return None


def problem(status: dict, owner: tuple[str, str] | None = None) -> str | None:
    """What stops this sign-in starting Remote Control, in words, or None.

    Only a claude.ai sign-in with no API key beside it can start it. Anything
    else makes `claude remote-control` exit at once, and the supervisor would
    restart it in a loop without ever saying why. The words match the
    entrypoint's, which says the same for the remote-control command.

    With *owner*, the account the box belongs to, any other account is a
    problem too: Remote Control would run its sessions with this box's keys.
    """
    if status.get("error"):
        return f"couldn't check the sign-in ({status['error']}); trying again"
    if status.get("loggedIn") is not True:
        return "not signed in to Claude yet: sign in on the manager page"
    if status.get("authMethod") == "claude.ai" and status.get("apiKeySource", "none") not in (None, "none"):
        return (f"signed in, but an API key is set as well ({status['apiKeySource']}), "
                "and Remote Control refuses that; remove it")
    if identity(status) is None:
        return (f"signed in with {status.get('authMethod') or 'an unknown method'}, which can't start "
                "Remote Control; it needs a claude.ai sign-in")
    if owner is not None and identity(status) != owner:
        return (f"signed in as {status['email']}, which isn't this box's account ({owner[0]}); "
                "sign the box out on the manager page")
    return None


def read_owner(path: Path) -> tuple[str, str] | None:
    """The (email, orgId) the box belongs to, or None before its first sign-in."""
    try:
        data = json.loads(path.read_text())
        return (data["email"], data["orgId"]) if isinstance(data, dict) else None
    except (OSError, ValueError, KeyError, TypeError):
        return None


def logout(config_dir: str | None = None, claude: str | None = None) -> None:
    """Run `claude auth logout`."""
    try:
        subprocess.run(
            [claude or CLAUDE, "auth", "logout"],
            env=_env(config_dir),
            capture_output=True,
            timeout=30,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        pass


class LoginFlow:
    """One `claude auth login`, run in a pseudo-terminal."""

    def __init__(self, config_dir: str | None = None, claude: str | None = None) -> None:
        self._config_dir = config_dir
        self._claude = claude
        self.proc: PtyProcess | None = None

    def start(self) -> None:
        """Start the login flow in a pseudo-terminal with 500 columns so the CLI never wraps the link."""
        self.proc = PtyProcess(
            [self._claude or CLAUDE, "auth", "login"],
            env=_env(self._config_dir),
            cols=500,
        )
        self.proc.start()

    def url(self, timeout: float = 20.0) -> str | None:
        """Poll for the login URL until it appears or timeout is reached."""
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            raw = self.proc.output().decode("utf-8", errors="replace")
            url = find_login_url(raw)
            if url is not None:
                return url
            time.sleep(0.1)
        return None

    def submit(self, code: str) -> None:
        """Submit a sign-in code after validating it."""
        code = code.strip()
        if not CODE.fullmatch(code):
            raise ValueError("That isn't a sign-in code: paste the code claude.ai showed you.")
        self.proc.write(code.encode() + b"\r")

    def wait(self, timeout: float = 60.0) -> bool:
        """Return True when the process exits with 0 within timeout."""
        if self.proc is None:
            return False
        exit_code = self.proc.wait(timeout=timeout)
        if exit_code is None:
            return False
        return exit_code == 0

    def cancel(self) -> None:
        """Terminate the login flow process if any."""
        if self.proc is not None:
            self.proc.terminate()

    def output(self) -> str:
        """Return the decoded output."""
        if self.proc is None:
            return ""
        return self.proc.output().decode("utf-8", errors="replace")
