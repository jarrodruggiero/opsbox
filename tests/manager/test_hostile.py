"""Every field of every route, sent what an attacker or a bad paste would send.

The route tests prove each route does its job with sensible input. This one
proves none of them falls over, echoes markup, or writes where it shouldn't
with input nobody planned for: a NUL, 5000 characters, markup, a header
injection, a path. It fails on a 500, on the raw markup coming back, on a
header the request smuggled in, and on any file changed outside the route's
own. After Stocktake's hostile-input walker.
"""
from __future__ import annotations

import pathlib

import pytest
import webtest
from webtest import Browser, raw, sign_in

from opsbox.manager import keys, render

MARKUP = "<script>alert(1)</script>\"'"
VALUES = [
    "\x00",
    "x" * 5000,
    MARKUP,
    "NaN",
    "%00",
    "a\r\nSet-Cookie: injected=1",
    "../../../etc/passwd",
    "‮gnp.exe",
    " ",
]

# Each route, the fields it reads besides csrf, and the files under HOME it
# may change. Sign-outs are walked separately: they end the session the walk
# depends on.
ENV_FILE = ".config/opsbox/env"
ROUTES: dict[str, tuple[list[str], set[str]]] = {
    "/rc/start": ([], set()),
    "/rc/stop": ([], set()),
    "/rc/restart": ([], set()),
    "/rc/trust": ([], set()),
    "/env": (["name", "value"], {ENV_FILE}),
    "/keys/ssh/generate": ([], {".ssh/id_ed25519", ".ssh/id_ed25519.pub"}),
}
for _key in keys.CATALOGUE:
    _own = {keys.PATHS[_key["id"]]} if _key["id"] in keys.PATHS else set()
    if _key["id"] == "local-llm":
        _own.add(ENV_FILE)
    if _key["id"] == "ssh":
        _own.add(".ssh/id_ed25519.pub")
    ROUTES[f"/keys/{_key['id']}"] = (["value"], _own)
    ROUTES[f"/keys/{_key['id']}/remove"] = ([], _own)


@pytest.fixture
def box(tmp_path: pathlib.Path, fake_claude: pathlib.Path, monkeypatch: pytest.MonkeyPatch):
    """The served box with the REAL page, so escaping is tested end to end, not the stub's JSON."""
    yield from webtest.serve_box(tmp_path, monkeypatch, render=render.page)


def snapshot(home: pathlib.Path) -> dict[str, bytes]:
    """Every file under home, by relative path, with its contents."""
    return {str(p.relative_to(home)): p.read_bytes() for p in home.rglob("*") if p.is_file()}


def changed(before: dict[str, bytes], after: dict[str, bytes]) -> set[str]:
    """The files added, removed or rewritten between two snapshots."""
    return {path for path in before.keys() | after.keys() if before.get(path) != after.get(path)}


def assert_safe(resp, where: str) -> None:
    """No server error, no raw markup back, no smuggled header."""
    assert resp.status < 500, f"{where}: {resp.status}"
    assert b"<script>alert(1)" not in resp.body, f"{where}: markup came back unescaped"
    assert not any("injected" in c for c in resp.set_cookies), f"{where}: a header was injected"
    assert "injected" not in str(resp.headers), f"{where}: a header was injected"


# The manager's own bookkeeping, which any request may touch.
BOOKKEEPING = {".config/opsbox/manager/sessions.json", ".config/opsbox/manager/secret"}


@pytest.mark.parametrize("route", sorted(ROUTES))
def test_every_field_survives_hostile_input(box, route: str) -> None:
    """A hostile value in any field of a signed-in route is refused or stored, never a crash or a stray write."""
    browser = Browser(box.port)
    assert sign_in(browser).status == 303
    fields, own = ROUTES[route]
    for field in fields or [None]:
        for value in VALUES if field else [""]:
            form = {name: "TZ" if name == "name" else "ok" for name in fields}
            if field is not None:
                form[field] = value
            form["csrf"] = browser.csrf()
            before = snapshot(box.home)
            resp = browser.post(route, form)
            where = f"{route} {field}={value[:20]!r}"
            assert_safe(resp, where)
            stray = changed(before, snapshot(box.home)) - own - BOOKKEEPING
            assert not stray, f"{where}: wrote {sorted(stray)}"
            # What it may write must still be readable: Remote Control's start
            # fails on a NUL in its environment, and a line break forges a name.
            env = box.home / ENV_FILE
            if env.exists():
                for line in env.read_text().splitlines():
                    name, sep, value = line.partition("=")
                    assert sep and name.isidentifier() and "\x00" not in value, f"{where}: env line {line!r}"


def test_hostile_csrf_and_codes_before_sign_in(box) -> None:
    """The sign-in forms take any csrf or code without a crash, and an odd one never signs anyone in."""
    browser = Browser(box.port)
    pre = browser.csrf()
    for value in VALUES:
        for route in ("/signin/start", "/signin/code", "/signin/cancel"):
            assert_safe(browser.post(route, {"csrf": value, "code": value}), f"{route} csrf={value[:20]!r}")
    assert browser.post("/signin/start", {"csrf": pre}).status == 303
    for value in VALUES:
        resp = browser.post("/signin/code", {"csrf": pre, "code": value})
        assert_safe(resp, f"/signin/code code={value[:20]!r}")
        assert resp.status == 400
    assert "opsbox_session" not in browser.cookies


def test_hostile_csrf_on_every_signed_in_route(box) -> None:
    """Any odd csrf on any signed-in route, the sign-outs included, is a 403 and changes nothing."""
    browser = Browser(box.port)
    assert sign_in(browser).status == 303
    routes = sorted(ROUTES) + ["/browsers/signout", "/browsers/signout-all", "/box/signout"]
    before = snapshot(box.home)
    for route in routes:
        for value in VALUES:
            resp = browser.post(route, {"csrf": value, "value": value, "name": value})
            assert_safe(resp, f"{route} csrf={value[:20]!r}")
            assert resp.status == 403, f"{route} csrf={value[:20]!r}: {resp.status}"
    assert changed(before, snapshot(box.home)) - BOOKKEEPING == set()
    assert box.rc.calls == ["start"]


# Not "//rc/start": http.server itself collapses leading slashes (CPython's
# open-redirect fix), so it reaches /rc/start, which is harmless.
@pytest.mark.parametrize("path", [
    "/keys/%2e%2e", "/keys/ssh%00", "/keys/../env", "/keys/kubernetes/../ssh/generate",
    "/keys//remove", "/keys/KUBERNETES", "/env/", "/rc/start/", "/rc/start?x=<script>alert(1)</script>",
])
def test_odd_paths_are_404s(box, path: str) -> None:
    """A path that only looks like a route is a 404 (or the exact route, query aside), never a crash."""
    browser = Browser(box.port)
    assert sign_in(browser).status == 303
    resp = browser.post(path, {"csrf": browser.csrf()})
    assert_safe(resp, path)
    if path.startswith("/rc/start?"):
        assert resp.status == 303
    else:
        assert resp.status == 404, f"{path}: {resp.status}"


@pytest.mark.parametrize("request_bytes", [
    b"GET / HTTP/1.0\r\nHost: 127.0.0.1\r\nCookie: " + b"opsbox_session=" + b"x" * 60000 + b"\r\n\r\n",
    b"GET / HTTP/1.0\r\nHost: 127.0.0.1\r\nCookie: opsbox_session=\x00; opsbox_presession=\xff\xfe\r\n\r\n",
    b"GET / HTTP/1.0\r\nHost: 127.0.0.1\x00.evil\r\n\r\n",
    b"GET / HTTP/1.0\r\nHost: \r\n\r\n",
    b"GET / HTTP/1.0\r\n\r\n",
    b"POST /signin/start HTTP/1.0\r\nHost: 127.0.0.1\r\nContent-Length: 5\r\n\r\n\xff\xfe\x00\x01\x02",
    b"POST /signin/start HTTP/1.0\r\nHost: 127.0.0.1\r\nContent-Length: 99999999999999999999\r\n\r\n",
    b"GET /static/%2e%2e%2fweb.py HTTP/1.0\r\nHost: 127.0.0.1\r\n\r\n",
])
def test_malformed_requests(box, request_bytes: bytes) -> None:
    """Requests no browser would send get an answer that isn't a 500, and nothing is echoed back."""
    response = raw(box.port, request_bytes)
    status_line = response.split(b"\r\n", 1)[0]
    assert status_line.startswith(b"HTTP/1."), response[:200]
    assert int(status_line.split()[1]) < 500, status_line
    assert b"evil" not in response
