"""Tests for the manager's HTTP server: sign-in, sessions, CSRF and the routes."""
from __future__ import annotations

import base64
import http.client
import json
import os
import pathlib
import socket
import threading
import urllib.parse
from types import SimpleNamespace

import pytest

from opsbox.manager import claude, web
from opsbox.manager.sessions import Sessions
from opsbox.manager.supervisor import Console


class StubRC:
    """Records what the page asked Remote Control to do."""

    def __init__(self) -> None:
        self.calls: list[str] = []
        self.trust_asked = False

    def start(self) -> None:
        self.calls.append("start")

    def stop(self) -> None:
        self.calls.append("stop")

    def restart(self) -> None:
        self.calls.append("restart")

    def answer_trust(self) -> bool:
        self.calls.append("trust")
        return self.trust_asked

    def status(self) -> dict:
        return {"state": "running"}


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

    def request(self, method: str, path: str, form: dict | None = None, headers: dict | None = None) -> SimpleNamespace:
        """Send one request and keep any cookies it sets."""
        sent = {"Host": self.host, "User-Agent": self.agent}
        if self.cookies:
            sent["Cookie"] = "; ".join(f"{name}={value}" for name, value in self.cookies.items())
        body = None
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

    def csrf(self) -> str:
        """The CSRF value the page would put in its forms."""
        return self.ctx()["csrf"]


def context_of(resp: SimpleNamespace) -> dict:
    """The context inside a page rendered by render_stub."""
    text = resp.body.decode()
    assert text.startswith("<html>"), text[:200]
    return json.loads(text[len("<html>"):-len("</html>")])


@pytest.fixture
def box(tmp_path: pathlib.Path, fake_claude: pathlib.Path, monkeypatch: pytest.MonkeyPatch):
    """A manager served on a free port, with a signed-out fake claude and a stub Remote Control."""
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
        render=render_stub, static_dir=static, hosts=["opsbox.home"],
    )
    server = web.serve(manager, host="127.0.0.1", port=0)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield SimpleNamespace(manager=manager, rc=rc, console=console, port=server.server_address[1], home=home)
    server.shutdown()
    server.server_close()


def sign_in(browser: Browser, code: str = "good-code-123") -> SimpleNamespace:
    """Start and finish a sign-in in this browser; the response to the code."""
    pre = browser.csrf()
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


def test_security_headers_on_every_response(box) -> None:
    """Every response carries every security header, including refusals and 404s."""
    browser = Browser(box.port)
    responses = [browser.get("/"), browser.get("/nope"), browser.post("/rc/start"), Browser(box.port, host="evil.example").get("/")]
    for resp in responses:
        for name, value in web.SECURITY_HEADERS.items():
            assert resp.headers.get(name) == value, (resp.status, name)


def test_wrong_host_is_refused(box) -> None:
    """A name other than an IP, localhost or a configured one gets 421, which stops DNS rebinding."""
    assert Browser(box.port, host="evil.example").get("/").status == 421
    assert Browser(box.port, host="evil.example").post("/signin/start").status == 421
    for host in ("127.0.0.1:8080", "localhost", "[::1]:8080", "10.1.1.124", "opsbox.home:8080"):
        assert Browser(box.port, host=host).get("/").status == 200, host


def test_first_signin_signs_the_box_in(box) -> None:
    """While the box is signed out, a browser's sign-in is the box's, and Remote Control is woken."""
    browser = Browser(box.port)
    pre = browser.csrf()
    assert browser.post("/signin/start", {"csrf": pre}).status == 303
    assert browser.ctx()["login"]["url"].startswith("https://claude.com/")

    resp = browser.post("/signin/code", {"csrf": pre, "code": "good-code-123"})
    assert resp.status == 303
    session_cookie = next(c for c in resp.set_cookies if c.startswith(web.SESSION_COOKIE + "="))
    for flag in ("HttpOnly", "SameSite=Strict", "Path=/", f"Max-Age={web.SESSION_MAX_AGE}"):
        assert flag in session_cookie
    assert claude.identity(claude.auth_status()) == ("me@example.com", "org-1")
    assert box.rc.calls == ["start"]
    ctx = browser.ctx()
    assert ctx["session"] is True
    assert ctx["flash"] == "Signed in."


def test_signin_without_presession_csrf_is_refused(box) -> None:
    """Another site can't start a sign-in: a missing, wrong or odd csrf gets 403, never a 500."""
    browser = Browser(box.port)
    browser.get("/")
    for form in ({}, {"csrf": "wrong"}, {"csrf": "é" * 43}):
        assert browser.post("/signin/start", form).status == 403, form
    stranger = Browser(box.port)
    stranger.cookies[web.PRESESSION_COOKIE] = "é" * 43
    assert stranger.post("/signin/start", {"csrf": "é" * 43}).status == 403
    assert box.manager.login is None


def test_signin_belongs_to_its_browser(box) -> None:
    """Only the browser that started a sign-in sees its link, and nobody else can finish or cancel it."""
    alice, mallory = Browser(box.port), Browser(box.port)
    pre = alice.csrf()
    assert alice.post("/signin/start", {"csrf": pre}).status == 303

    seen = mallory.ctx()["login"]
    assert seen == {"active": True, "mine": False, "url": None}
    theirs = mallory.csrf()
    assert mallory.post("/signin/code", {"csrf": theirs, "code": "good-code-123"}).status == 400
    assert mallory.post("/signin/cancel", {"csrf": theirs}).status == 303
    assert box.manager.login is not None, "another browser cancelled the sign-in"
    assert claude.auth_status()["loggedIn"] is False

    assert alice.post("/signin/code", {"csrf": pre, "code": "good-code-123"}).status == 303
    assert alice.ctx()["session"] is True


def test_second_browser_must_be_the_same_account(box, monkeypatch: pytest.MonkeyPatch) -> None:
    """Once the box is signed in, a browser gets a session only as the box's own Claude account."""
    assert sign_in(Browser(box.port)).status == 303

    log = box.home.parent / "claude.log"
    monkeypatch.setenv("FAKE_CLAUDE_LOG", str(log))
    monkeypatch.setenv("FAKE_CLAUDE_EMAIL", "other@example.com")
    stranger = Browser(box.port)
    resp = sign_in(stranger)
    assert resp.status == 403
    assert "different Claude account" in context_of(resp)["error"]
    assert web.SESSION_COOKIE not in stranger.cookies
    assert list(box.manager.signin_dir.iterdir()) == [], "the throwaway sign-in was kept"
    # Logged out as well as deleted, so the real CLI revokes its token.
    assert log.read_text().startswith(f"logout {box.manager.signin_dir}/")
    assert claude.identity(claude.auth_status()) == ("me@example.com", "org-1")

    monkeypatch.setenv("FAKE_CLAUDE_EMAIL", "me@example.com")
    second = Browser(box.port)
    assert sign_in(second).status == 303
    assert second.ctx()["session"] is True
    assert list(box.manager.signin_dir.iterdir()) == []
    assert claude.identity(claude.auth_status()) == ("me@example.com", "org-1")


def test_reauth_leaves_remote_control_alone(box) -> None:
    """A browser signing in to a signed-in box never starts, stops or restarts Remote Control."""
    assert sign_in(Browser(box.port)).status == 303
    assert box.rc.calls == ["start"]
    assert sign_in(Browser(box.port)).status == 303
    assert box.rc.calls == ["start"]


def test_signed_out_page_says_nothing_about_the_box(box) -> None:
    """Without a session the page learns only whether the box is signed in: no account, state or keys."""
    assert sign_in(Browser(box.port)).status == 303
    stranger = Browser(box.port)
    ctx = stranger.ctx()
    assert ctx["session"] is False
    assert ctx["box"] == {"loggedIn": True}
    assert ctx["rc"] == {} and ctx["keys"] == [] and ctx["env"] == {} and ctx["browsers"] == []
    assert ctx["csrf"] == stranger.cookies[web.PRESESSION_COOKIE]
    assert "me@example.com" not in json.dumps(ctx)


def test_actions_need_a_session_and_csrf(box) -> None:
    """A change needs both a session and that session's csrf; either alone gets 403."""
    browser = Browser(box.port)
    assert browser.post("/rc/restart", {"csrf": browser.csrf()}).status == 403
    assert sign_in(browser).status == 303
    for csrf in ("wrong", "é" * 64, browser.cookies[web.PRESESSION_COOKIE]):
        assert browser.post("/rc/restart", {"csrf": csrf}).status == 403
    assert "restart" not in box.rc.calls
    assert browser.post("/rc/restart", {"csrf": browser.csrf()}).status == 303
    assert box.rc.calls[-1] == "restart"


def test_api_and_console_need_a_session(box) -> None:
    """The status and the console are for signed-in browsers only."""
    browser = Browser(box.port)
    assert browser.get("/api/status").status == 401
    # The status line only: if the stream were opened, its keepalives would
    # keep a full read waiting forever.
    conn = http.client.HTTPConnection("127.0.0.1", box.port, timeout=10)
    try:
        conn.request("GET", "/console", headers={"Host": "127.0.0.1"})
        assert conn.getresponse().status == 401
    finally:
        conn.close()
    assert sign_in(browser).status == 303
    status = json.loads(browser.get("/api/status").body)
    assert status["rc"] == {"state": "running"} and status["box"]["email"] == "me@example.com"


def test_console_streams(box) -> None:
    """The console sends what it kept as a reset event, then each new chunk, as server-sent events."""
    browser = Browser(box.port)
    assert sign_in(browser).status == 303
    box.console.feed(b"before")
    conn = http.client.HTTPConnection("127.0.0.1", box.port, timeout=10)
    cookie = "; ".join(f"{name}={value}" for name, value in browser.cookies.items())
    conn.request("GET", "/console", headers={"Host": "127.0.0.1", "Cookie": cookie})
    resp = conn.getresponse()
    try:
        assert resp.status == 200
        assert resp.getheader("Content-Type") == "text/event-stream"
        assert resp.getheader("Content-Length") is None

        def event() -> list[str]:
            lines = []
            while (line := resp.fp.readline().decode().rstrip("\n")) != "":
                lines.append(line)
            return lines

        first = event()
        assert first[0] == "event: reset"
        assert base64.b64decode(first[1][len("data: "):]).endswith(b"before")
        box.console.feed(b"hello")
        assert event() == ["data: " + base64.b64encode(b"hello").decode()]
    finally:
        conn.close()


def test_bodies_are_bounded(box) -> None:
    """Too long a body gets 413, a negative length too (not a wait for the connection to close), and none gets 411."""
    assert raw(box.port, b"POST /rc/start HTTP/1.0\r\nHost: 127.0.0.1\r\nContent-Length: 200000\r\n\r\n").startswith(b"HTTP/1.0 413")
    assert raw(box.port, b"POST /rc/start HTTP/1.0\r\nHost: 127.0.0.1\r\nContent-Length: -1\r\n\r\n").startswith(b"HTTP/1.0 413")
    assert raw(box.port, b"POST /rc/start HTTP/1.0\r\nHost: 127.0.0.1\r\n\r\n").startswith(b"HTTP/1.0 411")


def test_static_only_known_names(box) -> None:
    """Only the named static files are served; nothing else in or above the directory."""
    browser = Browser(box.port)
    css = browser.get("/static/manager.css")
    assert css.status == 200 and css.headers["Content-Type"] == "text/css"
    # A file beside the static directory, which a traversal would reach.
    (box.home.parent / "secret.txt").write_text("secret")
    for path in ("/static/../secret.txt", "/static/%2e%2e/secret.txt", "/static/other.js", "/static/", "/static/manager.js"):
        assert browser.get(path).status == 404, path


def test_flash_is_a_fixed_message(box) -> None:
    """A flash cookie only ever selects one of the fixed messages; anything else shows nothing."""
    browser = Browser(box.port)
    browser.cookies[web.FLASH_COOKIE] = "<script>"
    resp = browser.get("/")
    assert context_of(resp)["flash"] is None
    assert b"<script>" not in resp.body
    browser.cookies[web.FLASH_COOKIE] = "saved"
    resp = browser.get("/")
    assert context_of(resp)["flash"] == "Saved."
    assert any(c.startswith(web.FLASH_COOKIE + "=;") and "Max-Age=0" in c for c in resp.set_cookies)


def test_key_routes(box) -> None:
    """Saving, generating and removing keys reach keys.py, and a refused key shows its reason with a 400."""
    browser = Browser(box.port)
    assert sign_in(browser).status == 303
    refused = browser.post("/keys/kubernetes", {"csrf": browser.csrf(), "value": "hello: world"})
    assert refused.status == 400
    assert "kubeconfig" in context_of(refused)["error"]

    assert browser.post("/keys/ssh/generate", {"csrf": browser.csrf()}).status == 303
    assert (box.home / ".ssh/id_ed25519").exists()
    ssh = next(k for k in browser.ctx()["keys"] if k["id"] == "ssh")
    assert ssh["set"] is True and ssh["public"].startswith("ssh-ed25519 ")

    assert browser.post("/keys/ssh/remove", {"csrf": browser.csrf()}).status == 303
    assert not (box.home / ".ssh/id_ed25519").exists()
    assert next(k for k in browser.ctx()["keys"] if k["id"] == "ssh")["set"] is False
    assert browser.post("/keys/nope", {"csrf": browser.csrf()}).status == 404
    assert browser.post("/keys/../env", {"csrf": browser.csrf()}).status == 404


def test_env_route(box) -> None:
    """Allowed settings are saved; others are refused with the reason."""
    browser = Browser(box.port)
    assert sign_in(browser).status == 303
    assert browser.post("/env", {"csrf": browser.csrf(), "name": "TZ", "value": "Australia/Melbourne"}).status == 303
    assert browser.ctx()["env"] == {"TZ": "Australia/Melbourne"}
    refused = browser.post("/env", {"csrf": browser.csrf(), "name": "PATH", "value": "/x"})
    assert refused.status == 400 and "isn't a setting" in context_of(refused)["error"]


def test_trust_route(box) -> None:
    """Trust is answered only when asked, and says so when it isn't."""
    browser = Browser(box.port)
    assert sign_in(browser).status == 303
    assert browser.post("/rc/trust", {"csrf": browser.csrf()}).status == 400
    box.rc.trust_asked = True
    assert browser.post("/rc/trust", {"csrf": browser.csrf()}).status == 303


def test_signouts(box) -> None:
    """This browser, every browser, and the box: each ends exactly what it says."""
    first, second = Browser(box.port), Browser(box.port)
    assert sign_in(first).status == 303 and sign_in(second).status == 303
    assert first.post("/browsers/signout", {"csrf": first.csrf()}).status == 303
    assert first.get("/api/status").status == 401 and second.get("/api/status").status == 200

    assert sign_in(first).status == 303
    assert first.post("/browsers/signout-all", {"csrf": first.csrf()}).status == 303
    assert first.get("/api/status").status == 401 and second.get("/api/status").status == 401
    assert claude.auth_status()["loggedIn"] is True

    assert sign_in(first).status == 303 and sign_in(second).status == 303
    assert first.post("/box/signout", {"csrf": first.csrf()}).status == 303
    assert box.rc.calls[-1] == "stop"
    assert claude.auth_status()["loggedIn"] is False
    # The other browser kept its cookie, and the session behind it is gone.
    assert web.SESSION_COOKIE in second.cookies
    assert second.get("/api/status").status == 401
    assert first.ctx()["box"] == {"loggedIn": False}


def test_an_unexpected_error_is_a_plain_500(box, monkeypatch: pytest.MonkeyPatch) -> None:
    """A bug gets a 500 whose body says nothing about it; the traceback goes to the log."""
    browser = Browser(box.port)
    assert sign_in(browser).status == 303

    def broken() -> dict:
        raise RuntimeError("internal-detail-7c2e")

    monkeypatch.setattr(box.rc, "status", broken)
    resp = browser.get("/")
    assert resp.status == 500
    assert b"internal-detail-7c2e" not in resp.body
    for name, value in web.SECURITY_HEADERS.items():
        assert resp.headers.get(name) == value


def test_no_signin_left_behind_when_claude_is_missing(box, monkeypatch: pytest.MonkeyPatch) -> None:
    """If claude can't start, the page says so and no half-started sign-in is left recorded."""
    monkeypatch.setattr(claude, "CLAUDE", os.devnull + "/missing")
    browser = Browser(box.port)
    resp = browser.post("/signin/start", {"csrf": browser.csrf()})
    assert resp.status == 400
    assert "sign-in link" in context_of(resp)["error"]
    assert box.manager.login is None
