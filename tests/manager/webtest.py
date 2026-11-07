"""Helpers for the tests that drive the manager over HTTP: a served box, and a browser."""
from __future__ import annotations

import http.client
import json
import pathlib
import re
import socket
import threading
import urllib.parse
from types import SimpleNamespace

import pytest

from opsbox.manager import keys, web
from opsbox.manager.sessions import Sessions
from opsbox.manager.shell import Shells
from opsbox.manager.supervisor import Console


class StubRC:
    """Records what the page asked Remote Control to do."""

    def __init__(self) -> None:
        self.calls: list[str] = []
        self.typed: list[bytes] = []
        self.sizes: list[tuple[int, int]] = []
        self.asking = False
        self.running = True

    def start(self) -> None:
        self.calls.append("start")

    def stop(self) -> None:
        self.calls.append("stop")

    def restart(self) -> bool:
        self.calls.append("restart")
        return self.running

    def answer(self, yes: bool, question: str) -> bool:
        self.calls.append(f"answer-{'yes' if yes else 'no'}")
        self.answered = question
        return self.asking

    def write(self, data: bytes) -> bool:
        self.typed.append(data)
        return self.running

    def resize(self, cols: int, rows: int) -> None:
        self.sizes.append((cols, rows))

    def status(self) -> dict:
        return {"state": "running", "restarts": 0, "last_exit": None, "started_at": 1.0, "pid": 1, "workdir": "/w", "question": None}


def render_stub(ctx: dict) -> str:
    """The page as JSON, so a test can read the context the server built."""
    return "<html>" + json.dumps(ctx, default=str) + "</html>"


class Browser:
    """An HTTP client that keeps its own cookies, as one browser would."""

    def __init__(self, port: int, *, host: str = "127.0.0.1", agent: str = "test-browser") -> None:
        self.port = port
        self.host = host
        self.agent = agent
        self.cookies: dict[str, str] = {}

    def request(self, method: str, path: str, form: dict | None = None, headers: dict | None = None, body: bytes | None = None) -> SimpleNamespace:
        """Send one request and keep any cookies it sets."""
        sent = {"Host": self.host, "User-Agent": self.agent}
        if self.cookies:
            sent["Cookie"] = "; ".join(f"{name}={value}" for name, value in self.cookies.items())
        if form is not None:
            body = urllib.parse.urlencode(form).encode()
            sent["Content-Type"] = "application/x-www-form-urlencoded"
        sent.update(headers or {})
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=90)
        try:
            conn.request(method, path, body=body, headers=sent)
            resp = conn.getresponse()
            data = resp.read()
        finally:
            conn.close()
        for cookie in resp.headers.get_all("Set-Cookie") or []:
            name, _, rest = cookie.partition("=")
            if "Max-Age=0" in cookie:
                self.cookies.pop(name, None)
            else:
                self.cookies[name] = rest.split(";", 1)[0]
        return SimpleNamespace(status=resp.status, headers=resp.headers, body=data, set_cookies=resp.headers.get_all("Set-Cookie") or [])

    def get(self, path: str, **kwargs) -> SimpleNamespace:
        return self.request("GET", path, **kwargs)

    def post(self, path: str, form: dict | None = None, **kwargs) -> SimpleNamespace:
        return self.request("POST", path, form if form is not None else {}, **kwargs)

    def ctx(self) -> dict:
        """GET / and return the context the page was rendered from."""
        resp = self.get("/")
        assert resp.status == 200, resp.body
        return context_of(resp)

    def upload(self, path: str, data: bytes, csrf: str | None = None, *, fields: dict | None = None,
               headers: dict | None = None) -> SimpleNamespace:
        """Send the page's upload form, as a browser's multipart body."""
        boundary = "----browserboundary7f3a"
        token = self.csrf() if csrf is None else csrf
        parts = []
        for name, value in (("csrf", token), ("path", path), *(fields or {}).items()):
            parts.append(f'--{boundary}\r\nContent-Disposition: form-data; name="{name}"\r\n\r\n{value}\r\n'.encode())
        parts.append(
            f'--{boundary}\r\nContent-Disposition: form-data; name="file"; filename="x"\r\n'
            "Content-Type: application/octet-stream\r\n\r\n".encode() + data + b"\r\n"
        )
        parts.append(f"--{boundary}--\r\n".encode())
        return self.request("POST", "/files", body=b"".join(parts), headers={"Content-Type": f"multipart/form-data; boundary={boundary}", **(headers or {})})

    def csrf(self) -> str:
        """The CSRF value the page would put in its forms, from the stub's context or the real page."""
        resp = self.get("/")
        assert resp.status == 200, resp.body
        if resp.body.startswith(b"<html>{"):
            ctx = context_of(resp)
            return ctx["csrf"] or ctx["presession"]
        return re.search(rb'name="csrf" value="([^"]+)"', resp.body).group(1).decode()


def context_of(resp: SimpleNamespace) -> dict:
    """The context inside a page rendered by render_stub."""
    text = resp.body.decode()
    assert text.startswith("<html>"), text[:200]
    return json.loads(text[len("<html>"):-len("</html>")])


def serve_box(tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch, render=render_stub):
    """A manager served on a free port, with a signed-out fake claude, a stub Remote Control and real /bin/sh shells."""
    home = tmp_path / "box"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("FAKE_CLAUDE_EMAIL", "me@example.com")
    static = tmp_path / "static"
    static.mkdir()
    (static / "manager.css").write_text("body {}")
    rc = StubRC()
    console = Console()
    manager = web.Manager(
        home=home, rc=rc, console=console, sessions=Sessions(home / ".config/opsbox/manager"),
        render=render, static_dir=static, hosts=["opsbox.home"],
        shells=Shells(workdir=str(home / "workspace"), env_file=keys.env_path(), argv=["/bin/sh"]),
    )
    server = web.serve(manager, host="127.0.0.1", port=0)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield SimpleNamespace(manager=manager, rc=rc, console=console, port=server.server_address[1], home=home)
    manager.shells.close_all()
    server.shutdown()
    server.server_close()


def sign_in(browser: Browser, code: str = "good-code-123") -> SimpleNamespace:
    """Start and finish a sign-in in this browser; the response to the code."""
    browser.get("/")
    # The pre-session cookie, whether or not this browser already has a session.
    pre = browser.cookies[web.PRESESSION_COOKIE]
    started = browser.post("/signin/start", {"csrf": pre})
    assert started.status == 303, started.body
    return browser.post("/signin/code", {"csrf": pre, "code": code})


def raw(port: int, request: bytes) -> bytes:
    """Send bytes as they are and return the response, for requests http.client won't make."""
    with socket.create_connection(("127.0.0.1", port), timeout=10) as sock:
        sock.sendall(request)
        chunks = []
        while chunk := sock.recv(65536):
            chunks.append(chunk)
    return b"".join(chunks)


