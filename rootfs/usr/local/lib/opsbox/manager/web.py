"""The manager's HTTP server: routes, security headers, browser sign-in, the
actions, a JSON status endpoint, and the live console stream.

The page has no accounts of its own. A browser earns a session by signing in
to Claude:
  - while the box is signed out, that sign-in IS the box's sign-in;
  - once it's signed in, a new browser signs in with a throwaway
    CLAUDE_CONFIG_DIR. It gets a session only if that account (email and
    orgId) is the box's. The throwaway sign-in is then logged out and deleted.
The box's own sign-in, and its Remote Control session, are never touched by a
browser's check.

A sign-in belongs to the browser that started it. Only that browser sees its
link and can finish or cancel it. Otherwise, during first setup, anyone on the
LAN who loaded the page could open the link, sign in with their own account,
and claim the box. Starting a new sign-in still cancels any other one, so
whoever finishes first setup owns the box: finish it promptly.

Plain HTTP, port 8080, by design. Anyone exposing it beyond the LAN puts HTTPS
in front of it.

Host check: a request whose Host is neither an IP address nor a name in
OPSBOX_MANAGER_HOSTS is refused. That defeats DNS rebinding, where another
site's name is pointed at this address.

CSRF: every state-changing request carries a CSRF token. Before there's a
session, a random pre-session cookie and a matching form field stand in for
it, so another site can't start or finish a sign-in on this box.

Never echo a request's text back unescaped, and never show a key's value.
Without a session the page learns nothing about the box beyond whether it's
signed in.
"""
from __future__ import annotations

import base64
import hmac
import http.server
import ipaddress
import json
import queue
import re
import secrets
import shutil
import sys
import tempfile
import threading
import time
import traceback
import urllib.parse
from pathlib import Path

from . import claude, keys

MAX_BODY = 131072
SESSION_COOKIE = "opsbox_session"
PRESESSION_COOKIE = "opsbox_presession"
FLASH_COOKIE = "opsbox_flash"
SESSION_MAX_AGE = 2592000

STATIC = {
    "manager.css": "text/css",
    "manager.js": "text/javascript",
    "xterm.js": "text/javascript",
    "xterm.css": "text/css",
}

MESSAGES = {
    "signed-in": "Signed in.",
    "browser-signed-out": "This browser is signed out.",
    "all-signed-out": "Every browser is signed out.",
    "box-signed-out": "The box is signed out of Claude.",
    "saved": "Saved.",
    "removed": "Removed.",
    "generated": "Generated.",
    "started": "Started.",
    "stopped": "Stopped.",
    "restarted": "Restarted.",
    "trusted": "Trusted.",
}

SECURITY_HEADERS = {
    "Content-Security-Policy": "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; connect-src 'self'; frame-ancestors 'none'; form-action 'self'; base-uri 'none'",
    "X-Content-Type-Options": "nosniff",
    "Referrer-Policy": "no-referrer",
    "X-Frame-Options": "DENY",
    "Cache-Control": "no-store",
}

# What secrets.token_urlsafe(32) makes. Anything else in the cookie is
# replaced rather than echoed back in Set-Cookie.
PRESESSION_SHAPE = re.compile(r"[A-Za-z0-9_-]{43}")
KEY_IDS = [entry["id"] for entry in keys.CATALOGUE]
WRONG_HOST = b"Use this box's address, or add the name to OPSBOX_MANAGER_HOSTS."


class WrongAccount(ValueError):
    """A browser signed in to a different Claude account from the box's."""


def _same(a: str, b: str) -> bool:
    """Constant-time comparison of two strings of any characters."""
    # As bytes: compare_digest raises on a str with any non-ASCII character.
    return bool(a) and bool(b) and hmac.compare_digest(a.encode(), b.encode())


def _clear(name: str) -> str:
    """A Set-Cookie value that deletes *name*."""
    return f"{name}=; Path=/; HttpOnly; SameSite=Strict; Max-Age=0"


class Manager:
    """The page's state: the box, Remote Control, browser sessions and any sign-in in progress."""

    def __init__(
        self,
        *,
        home: Path,
        rc,
        console,
        sessions,
        render,
        static_dir: Path,
        claude_path: str | None = None,
        hosts: list[str] | None = None,
    ) -> None:
        self.home = home
        self.rc = rc
        self.console = console
        self.sessions = sessions
        self.render = render
        self.static_dir = static_dir
        self.claude_path = claude_path
        self.hosts = hosts or []
        self.login = None
        self.login_kind: str | None = None
        self.login_dir: str | None = None
        self.login_url: str | None = None
        self.login_owner: str | None = None
        self._box_cache: tuple[float, dict] | None = None
        self._keys_cache: tuple[float, list[dict]] | None = None
        self.lock = threading.Lock()
        self.signin_dir = home / ".config/opsbox/manager/signin"

    def box_status(self) -> dict:
        """The box's `claude auth status`, cached for 5 seconds: every page load and status poll asks."""
        cached = self._box_cache
        if cached is not None and time.monotonic() - cached[0] < 5:
            return cached[1]
        status = claude.auth_status(claude=self.claude_path)
        self._box_cache = (time.monotonic(), status)
        return status

    def box_identity(self) -> tuple[str, str] | None:
        """The box's (email, orgId), or None when it isn't signed in to claude.ai."""
        return claude.identity(self.box_status())

    def describe_keys(self) -> list[dict]:
        """keys.describe_all(), cached for a minute, because `gh auth status` asks GitHub each time."""
        cached = self._keys_cache
        if cached is not None and time.monotonic() - cached[0] < 60:
            return cached[1]
        described = keys.describe_all()
        self._keys_cache = (time.monotonic(), described)
        return described

    def keys_changed(self) -> None:
        """Forget the described keys after any change to them."""
        self._keys_cache = None

    def signin_mine(self, owner: str | None) -> bool:
        """Whether a sign-in is in progress and *owner* (a pre-session value) started it."""
        # Read once: another thread may clear it between the check and the use.
        started_by = self.login_owner
        return self.login is not None and started_by is not None and _same(owner or "", started_by)

    def start_signin(self, owner: str) -> str:
        """Start signing in, cancelling any other sign-in; return the claude.ai link."""
        with self.lock:
            self._clear_flow()
            if self.box_identity() is None:
                kind, config_dir = "box", None
            else:
                kind = "browser"
                self.signin_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
                config_dir = tempfile.mkdtemp(dir=self.signin_dir)
            flow = claude.LoginFlow(config_dir, claude=self.claude_path)
            self.login, self.login_kind, self.login_dir, self.login_owner = flow, kind, config_dir, owner
            try:
                flow.start()
                url = flow.url(20)
            except OSError:
                url = None
            if url is None:
                self._clear_flow()
                raise ValueError("Claude didn't give a sign-in link. Try again.")
            self.login_url = url
            return url

    def finish_signin(self, code: str, label: str, owner: str) -> str:
        """Hand the code to Claude; return a new session token, or raise with what to do."""
        with self.lock:
            if not self.signin_mine(owner):
                raise ValueError("No sign-in is in progress in this browser. Start again.")
            # A malformed code raises ValueError here and leaves the sign-in
            # running, so the browser can paste it again.
            try:
                self.login.submit(code)
            except OSError:
                self._clear_flow()
                raise ValueError("That sign-in has ended. Start again.") from None
            try:
                ok = self.login.wait(60)
                if self.login_kind == "box":
                    self._box_cache = None
                    if not (ok and self.box_identity() is not None):
                        raise ValueError("Claude didn't accept that code. Start again.")
                    token = self.sessions.issue(label)
                    # It only wakes Remote Control; it never starts a second one.
                    self.rc.start()
                    return token
                browser = None
                if ok:
                    browser = claude.identity(claude.auth_status(config_dir=self.login_dir, claude=self.claude_path))
                claude.logout(config_dir=self.login_dir, claude=self.claude_path)
                if browser is None:
                    raise ValueError("Claude didn't accept that code. Start again.")
                if browser != self.box_identity():
                    raise WrongAccount("That's a different Claude account from the one this box uses.")
                return self.sessions.issue(label)
            finally:
                self._clear_flow()

    def cancel_signin(self, owner: str) -> None:
        """Cancel the sign-in in progress, but only for the browser that started it."""
        with self.lock:
            if self.signin_mine(owner):
                self._clear_flow()

    def sign_box_out(self) -> None:
        """Stop Remote Control, sign the box out of Claude, and end every browser's session."""
        self.rc.stop()
        claude.logout(claude=self.claude_path)
        self._box_cache = None
        self.sessions.revoke_all()

    def _clear_flow(self) -> None:
        """Cancel the flow, delete a browser flow's throwaway sign-in, and clear the fields."""
        if self.login is not None:
            self.login.cancel()
        if self.login_dir is not None:
            shutil.rmtree(self.login_dir, ignore_errors=True)
        self.login = self.login_kind = self.login_dir = self.login_url = self.login_owner = None


class Handler(http.server.BaseHTTPRequestHandler):
    """One request to the manager page."""

    server_version = "opsbox"
    sys_version = ""

    # -- logging -----------------------------------------------------------

    def log_request(self, code: int | str = "-", size: int | str = "-") -> None:
        """Log POSTs and failures; skip successful GETs, which the page polls every 5 seconds."""
        if self.command == "GET" and isinstance(code, int) and code < 400:
            return
        super().log_request(code, size)

    def log_message(self, format: str, *args: object) -> None:
        """One line to stderr, without any query string."""
        line = (format % args).split("?", 1)[0]
        print(f"opsbox-manager: {self.address_string()} {line}", file=sys.stderr, flush=True)

    # -- request helpers -----------------------------------------------------

    @property
    def manager(self) -> Manager:
        """The Manager this server runs."""
        return self.server.manager

    def _host_ok(self) -> bool:
        """Whether the Host header is an IP address, localhost, or a configured name."""
        host = self.headers.get("Host", "")
        if host.startswith("["):
            host = host[1:].split("]", 1)[0]
        else:
            host = host.rsplit(":", 1)[0]
        try:
            ipaddress.ip_address(host)
            return True
        except ValueError:
            return host == "localhost" or host in self.manager.hosts

    def _cookies(self) -> dict[str, str]:
        """The request's cookies, by name."""
        cookies: dict[str, str] = {}
        for part in self.headers.get("Cookie", "").split(";"):
            name, sep, value = part.strip().partition("=")
            if sep:
                cookies.setdefault(name, value)
        return cookies

    def _presession(self) -> str | None:
        """The pre-session cookie's value, when it has the shape this server makes."""
        value = self._cookies().get(PRESESSION_COOKIE, "")
        return value if PRESESSION_SHAPE.fullmatch(value) else None

    def _session(self) -> str | None:
        """The session token, when the cookie holds a valid one."""
        token = self._cookies().get(SESSION_COOKIE)
        return token if token and self.manager.sessions.valid(token) else None

    def _box(self, session: str | None) -> dict:
        """What the page may say about the box: without a session, only whether it's signed in."""
        status = self.manager.box_status()
        if not session:
            return {"loggedIn": bool(status.get("loggedIn"))}
        return {name: status.get(name) for name in ("loggedIn", "email", "orgName", "subscriptionType")}

    def _login(self, presession: str | None) -> dict:
        """The sign-in in progress, with its link only for the browser that started it."""
        mine = self.manager.signin_mine(presession)
        return {"active": self.manager.login is not None, "mine": mine, "url": self.manager.login_url if mine else None}

    # -- responses -------------------------------------------------------------

    def _headers(self, status: int, content_type: str, cookies: list[str] = (), length: int | None = None) -> None:
        """Send the status line and headers, the security headers included."""
        self.send_response(status)
        for name, value in SECURITY_HEADERS.items():
            self.send_header(name, value)
        self.send_header("Content-Type", content_type)
        if length is not None:
            self.send_header("Content-Length", str(length))
        for cookie in cookies:
            self.send_header("Set-Cookie", cookie)
        self.end_headers()
        self._responded = True

    def _send(self, status: int, body: bytes, content_type: str, *, cookies: list[str] = ()) -> None:
        """Send a complete response."""
        self._headers(status, content_type, cookies, len(body))
        self.wfile.write(body)

    def _text(self, status: int, body: bytes) -> None:
        """Send a plain-text response."""
        self._send(status, body, "text/plain; charset=utf-8")

    def _json(self, status: int, data: dict) -> None:
        """Send a JSON response."""
        self._send(status, json.dumps(data).encode(), "application/json")

    def _redirect(self, flash: str | None = None, cookies: list[str] = ()) -> None:
        """A 303 back to the page, with a flash message id when there is one."""
        cookies = list(cookies)
        if flash in MESSAGES:
            cookies.append(f"{FLASH_COOKIE}={flash}; Path=/; HttpOnly; SameSite=Strict; Max-Age=30")
        self.send_response(303)
        self.send_header("Location", "/")
        for name, value in SECURITY_HEADERS.items():
            self.send_header(name, value)
        self.send_header("Content-Length", "0")
        for cookie in cookies:
            self.send_header("Set-Cookie", cookie)
        self.end_headers()
        self._responded = True

    def _page(self, *, status: int = 200, error: str | None = None, cookies: list[str] = ()) -> None:
        """Render the page, with an error when an action was refused."""
        session = self._session()
        presession = self._presession()
        set_cookies = list(cookies)
        if presession is None:
            presession = secrets.token_urlsafe(32)
            set_cookies.append(f"{PRESESSION_COOKIE}={presession}; Path=/; HttpOnly; SameSite=Strict")
        flash = MESSAGES.get(self._cookies().get(FLASH_COOKIE, ""))
        if FLASH_COOKIE in self._cookies():
            set_cookies.append(_clear(FLASH_COOKIE))
        ctx = {
            "session": bool(session),
            "csrf": self.manager.sessions.csrf(session) if session else presession,
            "box": self._box(session),
            "rc": self.manager.rc.status() if session else {},
            "login": self._login(presession),
            "keys": self.manager.describe_keys() if session else [],
            # Only what the page manages: LOCAL_LLM_KEY_FILE belongs to its key.
            "env": {k: v for k, v in keys.get_env().items() if k in keys.ENV_ALLOWED} if session else {},
            "env_allowed": keys.ENV_ALLOWED if session else {},
            "browsers": self.manager.sessions.list() if session else [],
            "flash": flash,
            "error": error,
        }
        self._send(status, self.manager.render(ctx).encode(), "text/html; charset=utf-8", cookies=set_cookies)

    def _form(self) -> dict[str, str] | None:
        """The urlencoded body, first value per field; None after sending an error."""
        try:
            length = int(self.headers.get("Content-Length", ""))
        except ValueError:
            self._text(411, b"Send a Content-Length.")
            return None
        # A negative length would make read() wait for the connection to close.
        if length < 0 or length > MAX_BODY:
            self._text(413, b"That's too much to send.")
            return None
        body = self.rfile.read(length).decode("utf-8", errors="replace")
        try:
            fields = urllib.parse.parse_qs(body, keep_blank_values=True, max_num_fields=20)
        except ValueError:
            self._text(400, b"That form has too many fields.")
            return None
        return {name: values[0] for name, values in fields.items()}

    # -- dispatch ----------------------------------------------------------------

    def do_GET(self) -> None:
        """GET: the page, its static files, the status, and the console."""
        self._guarded(self._get)

    def do_POST(self) -> None:
        """POST: every change."""
        self._guarded(self._post)

    def _guarded(self, handle) -> None:
        """Run *handle*; an unexpected error gets a 500 and its traceback in the log, never the page."""
        self._responded = False
        try:
            if not self._host_ok():
                self._text(421, WRONG_HOST)
                return
            handle(urllib.parse.urlsplit(self.path).path)
        except (BrokenPipeError, ConnectionResetError):
            pass
        except Exception:
            traceback.print_exc(file=sys.stderr)
            if not self._responded:
                self._text(500, b"Something went wrong; the container log has the details.")

    def _get(self, path: str) -> None:
        """Route a GET by exact path."""
        if path == "/":
            self._page()
        elif path.startswith("/static/") and path[len("/static/"):] in STATIC:
            name = path[len("/static/"):]
            try:
                body = (self.manager.static_dir / name).read_bytes()
            except OSError:
                self._text(404, b"Not found.")
                return
            self._send(200, body, STATIC[name])
        elif path == "/api/status":
            session = self._session()
            if not session:
                self._json(401, {"error": "Sign in first."})
                return
            self._json(200, {"box": self._box(session), "rc": self.manager.rc.status(),
                             "login": {"active": self.manager.login is not None}})
        elif path == "/console":
            self._console()
        else:
            self._text(404, b"Not found.")

    def _console(self) -> None:
        """Server-sent events: everything kept so far as one "reset" event, then each chunk as it comes."""
        if not self._session():
            self._json(401, {"error": "Sign in first."})
            return
        console = self.manager.console
        backlog, q = console.attach()
        try:
            # No Content-Length: the stream lasts as long as the page is open,
            # and HTTP/1.0 ends it by closing the connection.
            self._headers(200, "text/event-stream")
            self.wfile.write(b"event: reset\ndata: " + base64.b64encode(backlog) + b"\n\n")
            self.wfile.flush()
            while True:
                try:
                    chunk = q.get(timeout=15)
                except queue.Empty:
                    self.wfile.write(b": keepalive\n\n")
                else:
                    if chunk is None:
                        return
                    self.wfile.write(b"data: " + base64.b64encode(chunk) + b"\n\n")
                self.wfile.flush()
        except OSError:
            pass
        finally:
            console.unsubscribe(q)

    def _post(self, path: str) -> None:
        """Route a POST by exact path, after the CSRF check."""
        form = self._form()
        if form is None:
            return
        if path in ("/signin/start", "/signin/code", "/signin/cancel"):
            self._signin(path, form)
            return
        session = self._session()
        if not session or not self.manager.sessions.check_csrf(session, form.get("csrf", "")):
            self._text(403, b"Sign in first, then try again.")
            return
        manager = self.manager
        try:
            if path == "/rc/start":
                manager.rc.start()
                self._redirect("started")
            elif path == "/rc/stop":
                manager.rc.stop()
                self._redirect("stopped")
            elif path == "/rc/restart":
                manager.rc.restart()
                self._redirect("restarted")
            elif path == "/rc/trust":
                if manager.rc.answer_trust():
                    self._redirect("trusted")
                else:
                    self._page(status=400, error="Remote Control isn't asking to trust a directory.")
            elif path == "/keys/ssh/generate":
                keys.generate_ssh()
                self._redirect("generated")
            elif path in {f"/keys/{key_id}" for key_id in KEY_IDS}:
                keys.set_key(path[len("/keys/"):], form.get("value", ""))
                self._redirect("saved")
            elif path in {f"/keys/{key_id}/remove" for key_id in KEY_IDS}:
                keys.remove_key(path[len("/keys/"):-len("/remove")])
                self._redirect("removed")
            elif path == "/env":
                keys.set_env(form.get("name", ""), form.get("value", ""))
                self._redirect("saved")
            elif path == "/browsers/signout":
                manager.sessions.revoke(session)
                self._redirect("browser-signed-out", [_clear(SESSION_COOKIE)])
            elif path == "/browsers/signout-all":
                manager.sessions.revoke_all()
                self._redirect("all-signed-out", [_clear(SESSION_COOKIE)])
            elif path == "/box/signout":
                manager.sign_box_out()
                self._redirect("box-signed-out", [_clear(SESSION_COOKIE)])
            else:
                self._text(404, b"Not found.")
        except keys.KeyError_ as refused:
            self._page(status=400, error=str(refused))
        finally:
            # After the change as well as before the page reloads, so a page
            # load in between can't keep the old description for a minute.
            if path.startswith("/keys/"):
                manager.keys_changed()

    def _signin(self, path: str, form: dict[str, str]) -> None:
        """The sign-in routes, which work before there's a session, so the pre-session cookie guards them."""
        presession = self._presession()
        if presession is None or not _same(presession, form.get("csrf", "")):
            self._text(403, b"This form has expired. Reload the page.")
            return
        manager = self.manager
        try:
            if path == "/signin/start":
                manager.start_signin(owner=presession)
                self._redirect()
            elif path == "/signin/code":
                label = self.headers.get("User-Agent", "a browser")[:120]
                token = manager.finish_signin(form.get("code", ""), label=label, owner=presession)
                self._redirect("signed-in", [f"{SESSION_COOKIE}={token}; Path=/; HttpOnly; SameSite=Strict; Max-Age={SESSION_MAX_AGE}"])
            else:
                manager.cancel_signin(owner=presession)
                self._redirect()
        except WrongAccount as refused:
            self._page(status=403, error=str(refused))
        except ValueError as refused:
            self._page(status=400, error=str(refused))


def serve(manager: Manager, host: str = "0.0.0.0", port: int = 8080) -> http.server.ThreadingHTTPServer:
    """Create the server (the caller runs serve_forever)."""
    server = http.server.ThreadingHTTPServer((host, port), Handler)
    server.daemon_threads = True
    server.manager = manager
    return server
