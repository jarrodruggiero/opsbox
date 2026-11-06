"""Tests for the manager's HTTP server: sign-in, sessions, CSRF and the routes."""
from __future__ import annotations

import base64
import http.client
import json
import os
import pathlib
import socket
import time

import pytest
from webtest import Browser, context_of, raw, sign_in

from opsbox.manager import claude, web


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
    assert ctx["box"] == {"loggedIn": True, "owned": True}
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
    assert first.ctx()["box"] == {"loggedIn": False, "owned": True}


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
    # A clean "signed out", so the sign-in itself is what fails to start.
    monkeypatch.setattr(claude, "auth_status", lambda **kwargs: {"loggedIn": False, "authMethod": "none"})
    monkeypatch.setattr(claude, "CLAUDE", os.devnull + "/missing")
    browser = Browser(box.port)
    resp = browser.post("/signin/start", {"csrf": browser.csrf()})
    assert resp.status == 400
    assert "sign-in link" in context_of(resp)["error"]
    assert box.manager.login is None


def test_an_unreadable_status_refuses_a_sign_in(box, monkeypatch: pytest.MonkeyPatch) -> None:
    """A status that couldn't be read is never "signed out": a box sign-in then would run over the box's own."""
    monkeypatch.setattr(claude, "auth_status", lambda **kwargs: {"loggedIn": False, "authMethod": "none", "error": "took too long"})
    browser = Browser(box.port)
    resp = browser.post("/signin/start", {"csrf": browser.csrf()})
    assert resp.status == 400
    assert "Couldn't check" in context_of(resp)["error"]
    assert box.manager.login is None


def test_the_box_stays_its_owners_after_a_sign_out(box, monkeypatch: pytest.MonkeyPatch) -> None:
    """Signing the box out doesn't hand it, and its keys, to the next account that signs in."""
    owner = Browser(box.port)
    assert sign_in(owner).status == 303
    assert owner.post("/box/signout", {"csrf": owner.csrf()}).status == 303

    monkeypatch.setenv("FAKE_CLAUDE_EMAIL", "other@example.com")
    stranger = Browser(box.port)
    resp = sign_in(stranger)
    assert resp.status == 403
    assert "different Claude account" in context_of(resp)["error"]
    assert web.SESSION_COOKIE not in stranger.cookies
    # Signed straight back out, and still the owner's.
    assert claude.auth_status()["loggedIn"] is False
    assert box.manager.owner() == ("me@example.com", "org-1")

    monkeypatch.setenv("FAKE_CLAUDE_EMAIL", "me@example.com")
    assert sign_in(owner).status == 303
    assert claude.identity(claude.auth_status()) == ("me@example.com", "org-1")


def test_a_box_signed_in_before_the_manager_is_adopted(box) -> None:
    """Upgrading from the CLI sign-in: the account already on the box becomes its owner, not the next visitor."""
    (pathlib.Path(os.environ["FAKE_CLAUDE_HOME"]) / "signed-in").write_text("me@example.com")
    assert Browser(box.port).ctx()["box"] == {"loggedIn": True, "owned": True}
    assert box.manager.owner() == ("me@example.com", "org-1")


def test_restart_with_nothing_running_says_so(box) -> None:
    """Restart while stopped changes nothing, and the page says why rather than "Restarted."."""
    browser = Browser(box.port)
    assert sign_in(browser).status == 303
    box.rc.running = False
    resp = browser.post("/rc/restart", {"csrf": browser.csrf()})
    assert resp.status == 400
    assert "nothing to restart" in context_of(resp)["error"]


def test_a_signed_out_browsers_console_stops(box) -> None:
    """An open console ends within seconds of its session being signed out, so a revoked session stops seeing output."""
    browser, other = Browser(box.port), Browser(box.port)
    assert sign_in(browser).status == 303 and sign_in(other).status == 303
    conn = http.client.HTTPConnection("127.0.0.1", box.port, timeout=10)
    cookie = "; ".join(f"{name}={value}" for name, value in browser.cookies.items())
    conn.request("GET", "/console", headers={"Host": "127.0.0.1", "Cookie": cookie})
    resp = conn.getresponse()
    try:
        assert resp.status == 200
        assert other.post("/browsers/signout-all", {"csrf": other.csrf()}).status == 303
        time.sleep(1.2)
        box.console.feed(b"after the sign-out")
        rest = resp.read()
        assert b"after the sign-out" not in base64.b64decode(b"".join(
            line[len(b"data: "):] for line in rest.split(b"\n") if line.startswith(b"data: ")) or b"")
    finally:
        conn.close()


def test_an_idle_connection_is_dropped(box, monkeypatch: pytest.MonkeyPatch) -> None:
    """A connection that sends nothing is closed, so idle sockets from one host can't hold every thread."""
    # The shipped value, then a short one so the test needn't wait for it.
    assert web.Handler.timeout is not None and 15 < web.Handler.timeout <= 60
    monkeypatch.setattr(web.Handler, "timeout", 1)
    with socket.create_connection(("127.0.0.1", box.port), timeout=10) as sock:
        started = time.monotonic()
        assert sock.recv(1) == b""
        assert time.monotonic() - started < 5


def test_host_names_ignore_case_and_spaces(tmp_path: pathlib.Path) -> None:
    """OPSBOX_MANAGER_HOSTS as typed (" OpsBox.Home, b.home") matches the names browsers send."""
    manager = web.Manager(home=tmp_path, rc=None, console=None, sessions=None, render=None, static_dir=tmp_path,
                          hosts=[" OpsBox.Home", " b.home ", ""])
    assert manager.hosts == ["opsbox.home", "b.home"]


def test_stale_signins_are_cleared(box, monkeypatch: pytest.MonkeyPatch) -> None:
    """Throwaway sign-ins a crash left in the volume are logged out and deleted at startup."""
    log = box.home.parent / "claude.log"
    monkeypatch.setenv("FAKE_CLAUDE_LOG", str(log))
    leftover = box.manager.signin_dir / "tmpleft"
    leftover.mkdir(parents=True)
    (leftover / "signed-in").write_text("me@example.com")
    box.manager.clear_stale_signins()
    assert list(box.manager.signin_dir.iterdir()) == []
    assert f"logout {leftover}" in log.read_text()


def test_a_code_after_the_cli_has_gone_says_start_again(box) -> None:
    """If claude auth login has already exited, pasting a code ends that sign-in with a reason, not a 500."""
    browser = Browser(box.port)
    pre = browser.csrf()
    assert browser.post("/signin/start", {"csrf": pre}).status == 303
    box.manager.login.cancel()
    box.manager.login.proc.wait(10)
    resp = browser.post("/signin/code", {"csrf": pre, "code": "good-code-123"})
    assert resp.status == 400
    assert "has ended" in context_of(resp)["error"]
    assert box.manager.login is None
