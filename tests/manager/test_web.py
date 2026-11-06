"""Tests for the manager's HTTP server: sign-in, sessions, CSRF and the routes."""
from __future__ import annotations

import base64
import http.client
import json
import os
import pathlib
import socket
import threading
import time

import pytest
from webtest import Browser, context_of, raw, sign_in

from opsbox.manager import claude, files, web
from opsbox.manager.sessions import session_id


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
    owner = Browser(box.port)
    assert sign_in(owner).status == 303
    # With something on the box to give away.
    assert owner.post("/env", {"csrf": owner.csrf(), "name": "TZ", "value": "UTC"}).status == 303
    assert owner.upload("~/.kube/config", b"data").status == 303
    assert owner.post("/ssh/generate", {"csrf": owner.csrf()}).status == 303
    stranger = Browser(box.port)
    ctx = stranger.ctx()
    assert ctx["session"] is False
    assert ctx["box"] == {"loggedIn": True, "owned": True}
    assert ctx["rc"] == {} and ctx["ssh"] == {} and ctx["files"] == [] and ctx["env"] == [] and ctx["browsers"] == []
    assert ctx["csrf"] == "" and ctx["presession"] == stranger.cookies[web.PRESESSION_COOKIE]
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
    assert status["rc"]["state"] == "running" and status["box"]["email"] == "me@example.com"


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


def test_ssh_routes(box) -> None:
    """Generate and Remove reach the key, and the page shows only its public half."""
    browser = Browser(box.port)
    assert sign_in(browser).status == 303
    assert browser.ctx()["ssh"] == {"set": False}
    assert browser.post("/ssh/generate", {"csrf": browser.csrf()}).status == 303
    assert (box.home / ".ssh/id_ed25519").exists()
    ssh = browser.ctx()["ssh"]
    assert ssh["public"].startswith("ssh-ed25519 ")
    refused = browser.post("/ssh/generate", {"csrf": browser.csrf()})
    assert refused.status == 400 and "already an SSH key" in context_of(refused)["error"]
    assert browser.post("/ssh/remove", {"csrf": browser.csrf()}).status == 303
    assert not (box.home / ".ssh/id_ed25519").exists()


def test_upload_saves_the_file_byte_for_byte(box) -> None:
    """Through a real multipart body, every byte arrives as sent, and the page lists it."""
    browser = Browser(box.port)
    assert sign_in(browser).status == 303
    data = bytes(range(256)) + b"\r\n--not-the-boundary\r\n\r\nend\r\n"
    resp = browser.upload("~/.kube/config", data)
    assert resp.status == 303
    saved = box.home / ".kube/config"
    assert saved.read_bytes() == data
    assert saved.stat().st_mode & 0o777 == 0o600
    ctx = browser.ctx()
    assert ctx["flash"] == "Uploaded."
    assert [row["path"] for row in ctx["files"]] == ["~/.kube/config"]


def test_upload_needs_a_session_and_its_csrf(box) -> None:
    """Without either, nothing is written; without a session, the body isn't even read."""
    stranger = Browser(box.port)
    assert stranger.upload("~/x", b"data", csrf="anything").status == 403
    browser = Browser(box.port)
    assert sign_in(browser).status == 303
    for csrf in ("wrong", "", browser.cookies[web.PRESESSION_COOKIE]):
        assert browser.upload("~/x", b"data", csrf=csrf).status == 403
    assert not (box.home / "x").exists()
    # Claims a body far over the limit and sends none: answered anyway, at once.
    reply = raw(box.port, b"POST /files HTTP/1.0\r\nHost: 127.0.0.1\r\nContent-Length: 999999\r\n\r\n")
    assert reply.startswith(b"HTTP/1.0 403")


def test_upload_refusals_say_why_on_the_page(box) -> None:
    """A bad path, an empty file and an oversize file each come back as a reason, and write nothing."""
    browser = Browser(box.port)
    assert sign_in(browser).status == 303
    for path, data, message in (
        ("/etc/passwd", b"data", "under the box's home"),
        ("~/.config/opsbox/manager/owner", b"data", "its own files"),
        ("~/empty", b"", "empty"),
        ("~/big", b"x" * (files.MAX_FILE + 70000), "limit"),
    ):
        resp = browser.upload(path, data)
        assert resp.status in (400, 413), path
        assert message in context_of(resp)["error"], path
    assert not (box.home / "empty").exists() and not (box.home / "big").exists()
    assert box.manager.owner() == ("me@example.com", "org-1"), "an upload reached the owner file"


def test_upload_that_isnt_multipart_is_refused(box) -> None:
    browser = Browser(box.port)
    assert sign_in(browser).status == 303
    assert browser.post("/files", {"csrf": browser.csrf(), "path": "~/x"}).status == 400
    assert not (box.home / "x").exists()


def test_a_cut_off_upload_doesnt_replace_a_good_file(box) -> None:
    """A dropped connection, or a body without its closing boundary, must leave what's there alone."""
    browser = Browser(box.port)
    assert sign_in(browser).status == 303
    assert browser.upload("~/.kube/config", b"the whole kubeconfig").status == 303
    token, cookie = browser.csrf(), "; ".join(f"{name}={value}" for name, value in browser.cookies.items())
    boundary = "----cut"
    start = (
        f'--{boundary}\r\nContent-Disposition: form-data; name="csrf"\r\n\r\n{token}\r\n'
        f'--{boundary}\r\nContent-Disposition: form-data; name="path"\r\n\r\n~/.kube/config\r\n'
        f'--{boundary}\r\nContent-Disposition: form-data; name="file"; filename="c"\r\n\r\nthe first half\r\n'
    ).encode()
    head = f"POST /files HTTP/1.0\r\nHost: 127.0.0.1\r\nCookie: {cookie}\r\nContent-Type: multipart/form-data; boundary={boundary}\r\n"
    # Everything it said it would send, but no closing boundary.
    reply = raw(box.port, (head + f"Content-Length: {len(start)}\r\n\r\n").encode() + start)
    assert reply.startswith(b"HTTP/1.0 400")
    # Fewer bytes than it said, then gone.
    with socket.create_connection(("127.0.0.1", box.port), timeout=10) as sock:
        sock.sendall((head + f"Content-Length: {len(start) + 500}\r\n\r\n").encode() + start)
        sock.shutdown(socket.SHUT_WR)
        assert sock.recv(64).startswith(b"HTTP/1.0 400")
    assert (box.home / ".kube/config").read_bytes() == b"the whole kubeconfig"


def test_a_cut_off_form_changes_nothing(box) -> None:
    """A setting's value mustn't be saved as however much of it arrived."""
    browser = Browser(box.port)
    assert sign_in(browser).status == 303
    token, cookie = browser.csrf(), "; ".join(f"{name}={value}" for name, value in browser.cookies.items())
    body = f"csrf={token}&name=TZ&value=Australia".encode()
    with socket.create_connection(("127.0.0.1", box.port), timeout=10) as sock:
        sock.sendall(f"POST /env HTTP/1.0\r\nHost: 127.0.0.1\r\nCookie: {cookie}\r\nContent-Length: {len(body) + 50}\r\n\r\n".encode() + body)
        sock.shutdown(socket.SHUT_WR)
        assert sock.recv(64).startswith(b"HTTP/1.0 400")
    assert browser.ctx()["env"] == []


def test_removing_a_file(box) -> None:
    """Remove reaches a file the page saved, and refuses anything else with the reason."""
    browser = Browser(box.port)
    assert sign_in(browser).status == 303
    assert browser.upload("~/mine", b"data").status == 303
    assert browser.post("/files/remove", {"csrf": browser.csrf(), "path": "~/mine"}).status == 303
    assert not (box.home / "mine").exists()
    (box.home / "other").write_text("kept")
    refused = browser.post("/files/remove", {"csrf": browser.csrf(), "path": "~/other"})
    assert refused.status == 400 and "only removes files it saved" in context_of(refused)["error"]
    assert (box.home / "other").read_text() == "kept"


def test_env_routes(box) -> None:
    """Any name and value can be saved, hidden ones never come back, and Remove works on whatever is there."""
    browser = Browser(box.port)
    assert sign_in(browser).status == 303
    assert browser.post("/env", {"csrf": browser.csrf(), "name": "ANYTHING_AT_ALL", "value": "yes"}).status == 303
    assert browser.post("/env", {"csrf": browser.csrf(), "name": "QUIET", "value": "SENTINEL-7f3a", "hidden": "1"}).status == 303
    resp = browser.get("/")
    assert b"SENTINEL-7f3a" not in resp.body
    assert context_of(resp)["env"] == [
        {"name": "ANYTHING_AT_ALL", "hidden": False, "value": "yes"},
        {"name": "QUIET", "hidden": True, "value": None},
    ]
    refused = browser.post("/env", {"csrf": browser.csrf(), "name": "ANTHROPIC_API_KEY", "value": "x"})
    assert refused.status == 400 and "claude.ai sign-in" in context_of(refused)["error"]
    assert browser.post("/env/remove", {"csrf": browser.csrf(), "name": "QUIET"}).status == 303
    assert [row["name"] for row in browser.ctx()["env"]] == ["ANYTHING_AT_ALL"]


def test_answer_route(box) -> None:
    """Yes and No reach Remote Control; anything else, or nothing being asked, says so."""
    browser = Browser(box.port)
    assert sign_in(browser).status == 303
    assert browser.post("/rc/answer", {"csrf": browser.csrf(), "answer": "yes"}).status == 400
    box.rc.asking = True
    assert browser.post("/rc/answer", {"csrf": browser.csrf(), "answer": "yes", "question": "Sure? (y/n)"}).status == 303
    assert box.rc.calls[-1] == "answer-yes"
    assert box.rc.answered == "Sure? (y/n)", "the question read on the page has to reach the check"
    assert browser.post("/rc/answer", {"csrf": browser.csrf(), "answer": "no"}).status == 303
    assert box.rc.calls[-1] == "answer-no"
    calls = len(box.rc.calls)
    assert browser.post("/rc/answer", {"csrf": browser.csrf(), "answer": "maybe"}).status == 400
    assert len(box.rc.calls) == calls


def test_typing_into_the_console(box) -> None:
    """Keystrokes reach Remote Control byte for byte, and need a session and its csrf like any change."""
    browser = Browser(box.port)
    assert browser.post("/console/input", {"csrf": browser.csrf(), "data": "x"}).status == 403
    assert sign_in(browser).status == 303
    assert browser.post("/console/input", {"csrf": "wrong", "data": "x"}).status == 403
    assert box.rc.typed == []
    keys_typed = "y\r\x03\x1b[A é"
    assert browser.post("/console/input", {"csrf": browser.csrf(), "data": keys_typed}).status == 204
    assert box.rc.typed == [keys_typed.encode()]
    box.rc.running = False
    assert browser.post("/console/input", {"csrf": browser.csrf(), "data": "x"}).status == 409
    assert browser.post("/console/input", {"csrf": browser.csrf(), "data": "x" * 5000}).status == 413


def test_resizing_the_console(box) -> None:
    browser = Browser(box.port)
    assert sign_in(browser).status == 303
    assert browser.post("/console/resize", {"csrf": browser.csrf(), "cols": "132", "rows": "40"}).status == 204
    assert box.rc.sizes == [(132, 40)]
    for cols, rows in (("x", "40"), ("0", "40"), ("132", "-1"), ("99999", "40"), ("", "")):
        assert browser.post("/console/resize", {"csrf": browser.csrf(), "cols": cols, "rows": rows}).status == 400
    assert box.rc.sizes == [(132, 40)]


def test_a_fetch_gets_no_redirect_and_no_flash_for_later(box) -> None:
    """manager.js updates the page itself: a 303 would make it fetch the page, and a flash would show on the next reload."""
    browser = Browser(box.port)
    assert sign_in(browser).status == 303
    browser.get("/")
    resp = browser.post("/rc/start", {"csrf": browser.csrf()}, headers={"X-Opsbox-Fetch": "1"})
    assert resp.status == 204
    assert not any(c.startswith(web.FLASH_COOKIE + "=started") for c in resp.set_cookies)
    box.rc.running = False
    refused = browser.post("/rc/restart", {"csrf": browser.csrf()}, headers={"X-Opsbox-Fetch": "1"})
    assert refused.status == 400 and "nothing to restart" in json.loads(refused.body)["error"]


def test_status_has_what_the_page_needs_to_update_itself(box) -> None:
    browser = Browser(box.port)
    assert sign_in(browser).status == 303
    rc = json.loads(browser.get("/api/status").body)["rc"]
    assert (rc["state"], rc["symbol"], rc["word"], rc["detail"], rc["question"]) == ("running", "\u25cf", "Running", "", None)


def read_event(resp) -> list[str]:
    """One server-sent event's lines, skipping keepalives."""
    lines: list[str] = []
    while True:
        line = resp.fp.readline().decode().rstrip("\n")
        if line == "" and lines:
            return lines
        if line and not line.startswith(":"):
            lines.append(line)


def open_stream(browser: Browser, path: str):
    conn = http.client.HTTPConnection("127.0.0.1", browser.port, timeout=10)
    cookie = "; ".join(f"{name}={value}" for name, value in browser.cookies.items())
    conn.request("GET", path, headers={"Host": "127.0.0.1", "Cookie": cookie})
    return conn, conn.getresponse()


def test_a_shell_over_the_page(box) -> None:
    """Open, type, read the answer in the stream, and see it end: the shell's whole round trip."""
    browser = Browser(box.port)
    assert sign_in(browser).status == 303
    assert browser.post("/shell/open", {"csrf": browser.csrf(), "cols": "90", "rows": "30"}).status == 204
    conn, resp = open_stream(browser, "/shell/stream")
    try:
        assert resp.status == 200
        assert read_event(resp)[0] == "event: reset"
        assert browser.post("/shell/input", {"csrf": browser.csrf(), "data": "echo round-$((20+1)); stty size; exit\r"}).status == 204
        seen = b""
        while True:
            lines = read_event(resp)
            if lines[0] == "event: end":
                break
            seen += base64.b64decode(lines[0][len("data: "):])
        assert b"round-21" in seen
        assert b"30 90" in seen
    finally:
        conn.close()
    assert browser.post("/shell/input", {"csrf": browser.csrf(), "data": "x"}).status == 409


def test_shells_need_a_session_csrf_and_one_of_their_own(box) -> None:
    """A browser reaches only its own shell, and nothing here works without a session."""
    stranger = Browser(box.port)
    assert stranger.post("/shell/open", {"csrf": stranger.csrf(), "cols": "90", "rows": "30"}).status == 403
    conn, resp = open_stream(stranger, "/shell/stream")
    assert resp.status == 401
    conn.close()

    mine, theirs = Browser(box.port), Browser(box.port)
    assert sign_in(mine).status == 303 and sign_in(theirs).status == 303
    assert mine.post("/shell/open", {"csrf": "wrong", "cols": "90", "rows": "30"}).status == 403
    conn, resp = open_stream(mine, "/shell/stream")
    assert resp.status == 404
    conn.close()

    assert mine.post("/shell/open", {"csrf": mine.csrf(), "cols": "90", "rows": "30"}).status == 204
    assert theirs.post("/shell/input", {"csrf": theirs.csrf(), "data": "x"}).status == 409
    conn, resp = open_stream(theirs, "/shell/stream")
    assert resp.status == 404
    conn.close()


def test_signing_out_ends_the_shell(box) -> None:
    """A browser's shell goes with its session, whichever way the session ends."""
    def opened() -> tuple[Browser, object]:
        browser = Browser(box.port)
        assert sign_in(browser).status == 303
        assert browser.post("/shell/open", {"csrf": browser.csrf(), "cols": "90", "rows": "30"}).status == 204
        return browser, box.manager.shells.get(session_id(browser.cookies[web.SESSION_COOKIE]))

    browser, shell = opened()
    assert shell.running()
    assert browser.post("/browsers/signout", {"csrf": browser.csrf()}).status == 303
    assert not shell.running()

    browser, shell = opened()
    other, other_shell = opened()
    their_id = session_id(other.cookies[web.SESSION_COOKIE])
    assert browser.post("/browsers/signout", {"csrf": browser.csrf(), "id": their_id}).status == 303
    assert not other_shell.running() and shell.running()
    assert other.get("/api/status").status == 401 and browser.get("/api/status").status == 200

    assert browser.post("/browsers/signout-all", {"csrf": browser.csrf()}).status == 303
    assert not shell.running()

    browser, shell = opened()
    assert browser.post("/box/signout", {"csrf": browser.csrf()}).status == 303
    assert not shell.running()


def test_the_browsers_list_marks_this_one(box) -> None:
    first, second = Browser(box.port, agent="first"), Browser(box.port, agent="second")
    assert sign_in(first).status == 303 and sign_in(second).status == 303
    rows = {row["label"]: row["current"] for row in first.ctx()["browsers"]}
    assert rows == {"first": True, "second": False}


def test_a_shell_can_be_picked_up_again_after_a_reload(box) -> None:
    """A second stream replays what the first one saw: the same shell, not a new one."""
    browser = Browser(box.port)
    assert sign_in(browser).status == 303
    assert browser.post("/shell/open", {"csrf": browser.csrf(), "cols": "90", "rows": "30"}).status == 204
    assert browser.post("/shell/input", {"csrf": browser.csrf(), "data": "echo before-$((1+1))\r"}).status == 204
    shell = box.manager.shells.get(session_id(browser.cookies[web.SESSION_COOKIE]))
    deadline = time.monotonic() + 5
    while b"before-2" not in shell.console.output() and time.monotonic() < deadline:
        time.sleep(0.02)
    assert browser.post("/shell/open", {"csrf": browser.csrf(), "cols": "90", "rows": "30"}).status == 204
    conn, resp = open_stream(browser, "/shell/stream")
    try:
        assert b"before-2" in base64.b64decode(read_event(resp)[1][len("data: "):])
    finally:
        conn.close()
    assert box.manager.shells.get(session_id(browser.cookies[web.SESSION_COOKIE])) is shell


def test_a_viewer_is_counted_while_the_stream_is_open(box) -> None:
    """The idle clock is stopped by a real stream, and restarts when it goes: nothing calls these by hand."""
    browser = Browser(box.port)
    assert sign_in(browser).status == 303
    assert browser.post("/shell/open", {"csrf": browser.csrf(), "cols": "90", "rows": "30"}).status == 204
    shell = box.manager.shells.get(session_id(browser.cookies[web.SESSION_COOKIE]))
    assert shell.idle_for() > 0
    conn, resp = open_stream(browser, "/shell/stream")
    read_event(resp)
    assert shell.idle_for() == 0
    resp.close()
    conn.close()
    # The server finds out on its next write.
    deadline = time.monotonic() + 10
    while shell.idle_for() == 0 and time.monotonic() < deadline:
        shell.write(b"echo x\r")
        time.sleep(0.1)
    assert shell.idle_for() > 0


def test_status_says_whether_this_browsers_shell_is_there(box) -> None:
    """The page decides from this whether to reconnect or say the shell has ended."""
    browser = Browser(box.port)
    assert sign_in(browser).status == 303
    assert json.loads(browser.get("/api/status").body)["shell"] == {"open": False}
    assert browser.post("/shell/open", {"csrf": browser.csrf(), "cols": "90", "rows": "30"}).status == 204
    assert json.loads(browser.get("/api/status").body)["shell"] == {"open": True}


def test_an_unreadable_status_isnt_reported_as_signed_out(box, monkeypatch: pytest.MonkeyPatch) -> None:
    """One slow `claude auth status` mustn't make the page reload, or offer a sign-in over the box's own."""
    browser = Browser(box.port)
    assert sign_in(browser).status == 303
    monkeypatch.setattr(claude, "auth_status", lambda **kwargs: {"loggedIn": False, "authMethod": "none", "error": "took too long"})
    box.manager.forget_box_status()
    assert json.loads(browser.get("/api/status").body)["box"]["unknown"] is True
    assert browser.ctx()["box"]["unknown"] is True


def test_a_stranger_cant_multiply_status_processes(box, monkeypatch: pytest.MonkeyPatch) -> None:
    """Each check is a process and anyone on the LAN can ask: a crowd of page loads runs one."""
    runs = []

    def slow(**kwargs) -> dict:
        runs.append(1)
        time.sleep(0.4)
        return {"loggedIn": False, "authMethod": "none"}

    monkeypatch.setattr(claude, "auth_status", slow)
    box.manager.forget_box_status()
    replies = []
    threads = [threading.Thread(target=lambda: replies.append(Browser(box.port).get("/").status)) for _ in range(12)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert replies == [200] * 12
    assert len(runs) == 1


def test_a_check_begun_before_a_sign_in_doesnt_count_after_it(box, monkeypatch: pytest.MonkeyPatch) -> None:
    """Or a poll that started while signed out would make a good sign-in look refused."""
    release = threading.Event()
    started = threading.Event()
    real = claude.auth_status

    def held(**kwargs) -> dict:
        status = real(**kwargs)
        started.set()
        release.wait(5)
        return status

    monkeypatch.setattr(claude, "auth_status", held)
    box.manager.forget_box_status()
    old_check = threading.Thread(target=box.manager.box_status)
    old_check.start()
    started.wait(5)
    box.manager.forget_box_status()
    release.set()
    old_check.join()
    assert box.manager._box_cache is None


def test_one_address_cant_hold_every_connection(box, monkeypatch: pytest.MonkeyPatch) -> None:
    """Idle or dripping connections from one host take a thread each, and only ever crowd out that host."""
    monkeypatch.setattr(web, "MAX_CONNECTIONS_PER_CLIENT", 3)
    held = [socket.create_connection(("127.0.0.1", box.port), timeout=10) for _ in range(3)]
    try:
        time.sleep(0.2)
        with socket.create_connection(("127.0.0.1", box.port), timeout=10) as extra:
            assert extra.recv(1) == b""
    finally:
        for sock in held:
            sock.close()
    # Freed as they close.
    deadline = time.monotonic() + 35
    while time.monotonic() < deadline:
        try:
            if Browser(box.port).get("/").status == 200:
                return
        except OSError:
            time.sleep(0.2)
    pytest.fail("the connections were never freed")


def test_a_stranger_cant_write_escape_sequences_or_much_else_to_the_log(box, capsys: pytest.CaptureFixture) -> None:
    """The log is read in a terminal, and Docker keeps all of it."""
    capsys.readouterr()
    raw(box.port, b"GET /\x1b]0;owned\x07" + b"x" * 5000 + b" HTTP/1.0\r\nHost: 127.0.0.1\r\n\r\n")
    log = capsys.readouterr().err
    assert "\x1b" not in log and "\x07" not in log
    assert "owned" in log
    assert max(len(line) for line in log.splitlines()) < 300


def test_a_request_line_too_broken_to_have_a_path_gets_an_answer(box, capsys: pytest.CaptureFixture) -> None:
    """Not a traceback in the log: stdlib sets no path on it, and logging it mustn't assume one."""
    capsys.readouterr()
    # An HTTP/0.9 reply, by stdlib's reading of it: the error page, with no status line.
    assert b"Error code: 400" in raw(box.port, b"GET / HTTX/1.0\r\n\r\n")
    assert "Traceback" not in capsys.readouterr().err


def test_keystrokes_stay_out_of_the_log(box, capsys: pytest.CaptureFixture) -> None:
    """A line per key would bury the log, and what's typed in a terminal is nobody's business."""
    browser = Browser(box.port)
    assert sign_in(browser).status == 303
    capsys.readouterr()
    browser.post("/console/input", {"csrf": browser.csrf(), "data": "x"})
    browser.post("/console/resize", {"csrf": browser.csrf(), "cols": "90", "rows": "30"})
    assert browser.post("/rc/start", {"csrf": browser.csrf()}).status == 303
    log = capsys.readouterr().err
    assert "/console/" not in log
    assert "/rc/start" in log


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
