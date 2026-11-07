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

The box belongs to the account of its first sign-in, recorded in
~/.config/opsbox/manager/owner (or adopted from a box already signed in). After
that, a sign-in as any other account is refused, even while the box is signed
out, so signing the box out never hands it and its keys to the next visitor.
Changing the account means signing the box out, then deleting that file from a
shell on the box. And a
status that can't be read is never taken for "signed out".

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

Never echo a request's text back unescaped, and never show a hidden setting's
value or a file's contents. Without a session the page learns nothing about the
box beyond whether it's signed in.

A session is the box's own user: it can open a shell and write files. That's what
it's for, and why everything above guards who gets one.
"""
from __future__ import annotations

import base64
import email
import email.policy
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

from . import claude, files, keys
from .sessions import _write_private, session_id
from .supervisor import STATE_LABELS, detail_line

MAX_BODY = 131072
# How much of an upload to read at all. More than a file may be (files.save
# refuses those), so an oversize file gets its reason rather than a reset connection.
MAX_UPLOAD = 16 * files.MAX_FILE
MAX_INPUT = 4096
# A multipart body's parts (csrf, path, file and room to spare), and the most any
# one's headers need: parsing costs time before the csrf can even be checked.
MAX_PARTS = 8
MAX_PART_HEADERS = 4096
# A thread each, and parked headers can hold megabytes. Per address, so that one
# host can only crowd out itself; and in all, as a floor under everything else.
MAX_CONNECTIONS_PER_CLIENT = 24
MAX_CONNECTIONS = 256
LOG_LINE_LIMIT = 200
SESSION_COOKIE = "opsbox_session"
PRESESSION_COOKIE = "opsbox_presession"
FLASH_COOKIE = "opsbox_flash"
SESSION_MAX_AGE = 2592000

STATIC = {
    "manager.css": "text/css",
    "manager.js": "text/javascript",
    "xterm.js": "text/javascript",
    "xterm.css": "text/css",
    "xterm-addon-fit.js": "text/javascript",
    "icon.svg": "image/svg+xml",
}

MESSAGES = {
    "signed-in": "Signed in.",
    "browser-signed-out": "This browser is signed out.",
    "all-signed-out": "Every browser is signed out.",
    "box-signed-out": "The box is signed out of Claude.",
    "other-signed-out": "That browser is signed out.",
    "saved": "Saved.",
    "uploaded": "Uploaded.",
    "removed": "Removed.",
    "generated": "Generated.",
    "answered": "Answered.",
    "started": "Started.",
    "stopped": "Stopped.",
    "restarted": "Restarted.",
}

SECURITY_HEADERS = {
    "Content-Security-Policy": "default-src 'self'; script-src 'self'; style-src 'self'; style-src-elem 'self' 'unsafe-inline'; img-src 'self' data:; connect-src 'self'; frame-ancestors 'none'; form-action 'self'; base-uri 'none'",
    "X-Content-Type-Options": "nosniff",
    "Referrer-Policy": "no-referrer",
    "X-Frame-Options": "DENY",
    "Cache-Control": "no-store",
}

# style-src-elem allows the <style> elements xterm.js writes for its theme and
# cell sizes; without them the console has no colours and its columns drift.
# Style attributes and every script stay 'self' only.

# What secrets.token_urlsafe(32) makes. Anything else in the cookie is
# replaced rather than echoed back in Set-Cookie.
PRESESSION_SHAPE = re.compile(r"[A-Za-z0-9_-]{43}")
# Keystrokes and terminal sizes: a line per key in the container's log would
# bury everything else, and keystrokes are never its business.
QUIET_ROUTES = {"/console/input", "/console/resize", "/shell/input", "/shell/resize"}
WRONG_HOST = b"Use this box's address, or add the name to OPSBOX_MANAGER_HOSTS."


class WrongAccount(ValueError):
    """A browser signed in to a different Claude account from the box's."""


def _same(a: str, b: str) -> bool:
    """Constant-time comparison of two strings of any characters."""
    # As bytes: compare_digest raises on a str with any non-ASCII character.
    return bool(a) and bool(b) and hmac.compare_digest(a.encode(), b.encode())


def _multipart(content_type: str, body: bytes) -> dict[str, bytes] | None:
    """A multipart/form-data body's fields, byte for byte, or None if it isn't a whole one.

    Split by hand rather than handed to email's parser, which rewrites line
    endings it finds in a part. A file has to arrive exactly as it was, and an
    upload without its closing boundary was cut short: saving it would replace a
    good file with part of one.
    """
    if len(content_type) > MAX_PART_HEADERS:
        return None
    head = email.message_from_string(f"Content-Type: {content_type}\n\n", policy=email.policy.HTTP)
    boundary = head.get_param("boundary") if head.get_content_type() == "multipart/form-data" else None
    if not isinstance(boundary, str) or not boundary:
        return None
    fields: dict[str, bytes] = {}
    for part in body.split(b"--" + boundary.encode("latin-1", "replace"), MAX_PARTS + 1)[1:]:
        if part.startswith(b"--"):
            return fields
        headers, separator, payload = part.partition(b"\r\n\r\n")
        if not separator or len(headers) > MAX_PART_HEADERS:
            return None
        disposition = email.message_from_bytes(headers.lstrip() + b"\r\n\r\n", policy=email.policy.HTTP)
        name = disposition.get_param("name", header="content-disposition")
        if isinstance(name, str):
            fields.setdefault(name, payload.removesuffix(b"\r\n"))
    return None


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
        shells=None,
    ) -> None:
        self.home = home
        self.rc = rc
        self.console = console
        self.sessions = sessions
        self.render = render
        self.static_dir = static_dir
        self.claude_path = claude_path
        self.shells = shells
        # Host names compare case-insensitively, and a list typed as
        # "a.home, b.home" means two names.
        self.hosts = [h.strip().lower() for h in hosts or [] if h.strip()]
        self.login = None
        self.login_kind: str | None = None
        self.login_dir: str | None = None
        self.login_url: str | None = None
        self.login_owner: str | None = None
        self._box_cache: tuple[float, dict] | None = None
        self._box_lock = threading.Lock()
        self._box_generation = 0
        self.lock = threading.Lock()
        self.signin_dir = home / ".config/opsbox/manager/signin"
        self.owner_path = home / ".config/opsbox/manager/owner"

    def owner(self) -> tuple[str, str] | None:
        """The (email, orgId) this box belongs to, or None before its first sign-in."""
        return claude.read_owner(self.owner_path)

    def _record_owner(self, account: tuple[str, str]) -> None:
        """Pin the box to *account*, unless it already belongs to one."""
        if self.owner() is None:
            _write_private(self.owner_path, json.dumps({"email": account[0], "orgId": account[1]}))

    def clear_stale_signins(self) -> None:
        """Log out and delete throwaway sign-ins a crash or restart left behind in the volume."""
        if not self.signin_dir.is_dir():
            return
        for leftover in self.signin_dir.iterdir():
            # Never follow a link out of the directory.
            if leftover.is_dir() and not leftover.is_symlink():
                claude.logout(config_dir=str(leftover), claude=self.claude_path)
                shutil.rmtree(leftover, ignore_errors=True)
            else:
                leftover.unlink(missing_ok=True)

    def box_status(self) -> dict:
        """The box's `claude auth status`, cached for 5 seconds: every page load and status poll asks."""
        cached = self._box_cache
        if cached is not None and time.monotonic() - cached[0] < 5:
            return cached[1]
        # One at a time: each is a process, and anyone on the LAN can ask. Others
        # get the last answer while it runs, or wait for it if there isn't one.
        if not self._box_lock.acquire(blocking=cached is None):
            return cached[1]
        try:
            cached = self._box_cache
            if cached is not None and time.monotonic() - cached[0] < 5:
                return cached[1]
            generation = self._box_generation
            status = claude.auth_status(claude=self.claude_path)
            # Not if the box's sign-in changed while that ran: it may predate it.
            if generation == self._box_generation:
                self._box_cache = (time.monotonic(), status)
        finally:
            self._box_lock.release()
        # A box signed in before the manager existed (the CLI, an older image)
        # is adopted: its account becomes the owner.
        account = claude.identity(status)
        if account is not None:
            self._record_owner(account)
        return status

    def forget_box_status(self) -> None:
        """The box's sign-in has just changed: nothing read before now counts."""
        self._box_generation += 1
        self._box_cache = None

    def box_identity(self) -> tuple[str, str] | None:
        """The box's (email, orgId), or None when it isn't signed in to claude.ai."""
        return claude.identity(self.box_status())

    def signin_mine(self, owner: str | None) -> bool:
        """Whether a sign-in is in progress and *owner* (a pre-session value) started it."""
        # Read once: another thread may clear it between the check and the use.
        started_by = self.login_owner
        return self.login is not None and started_by is not None and _same(owner or "", started_by)

    def start_signin(self, owner: str) -> str:
        """Start signing in, cancelling any other sign-in; return the claude.ai link."""
        with self.lock:
            self._clear_flow()
            if self.box_status().get("error"):
                # Unknown is not signed out: a box sign-in now would run over
                # the box's own credentials.
                raise ValueError("Couldn't check the box's sign-in; try again.")
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
                    self.forget_box_status()
                    account = claude.identity(self.box_status()) if ok else None
                    if account is None:
                        raise ValueError("Claude didn't accept that code. Start again.")
                    owner = self.owner()
                    if owner is not None and account != owner:
                        # Undo it before anything uses it; the supervisor
                        # also refuses to start Remote Control as this account.
                        claude.logout(claude=self.claude_path)
                        self.forget_box_status()
                        raise WrongAccount("That's a different Claude account from the one this box uses.")
                    self._record_owner(account)
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
                if browser != (self.owner() or self.box_identity()):
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
        self.forget_box_status()
        self.sessions.revoke_all()
        self.shells.close_all()

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
    # Seconds a connection may sit without sending or reading anything. Without
    # it, a thousand idle connections from one LAN host hold a thousand threads
    # and the page stops answering. The console's keepalive comes every 15.
    timeout = 30
    # A console message mustn't wait for the last one to be acknowledged, which a
    # client can take up to 200 ms over, or keystrokes echo late and output stutters.
    disable_nagle_algorithm = True

    # -- logging -----------------------------------------------------------

    def log_request(self, code: int | str = "-", size: int | str = "-") -> None:
        """Log changes and failures; skip what the page does constantly: its polls and keystrokes."""
        # No path at all on a request line too broken to have one.
        quiet = self.command == "GET" or getattr(self, "path", "") in QUIET_ROUTES
        if quiet and isinstance(code, int) and code < 400:
            return
        super().log_request(code, size)

    def log_message(self, format: str, *args: object) -> None:
        """One short line to stderr, without any query string: a stranger chooses the path it holds."""
        line = (format % args).split("?", 1)[0]
        # Escape sequences would drive the terminal of whoever reads the log.
        line = re.sub(r"[\x00-\x1f\x7f-\x9f]", lambda found: f"\\x{ord(found[0]):02x}", line)[:LOG_LINE_LIMIT]
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
        host = host.lower()
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
        """What the page may say about the box: without a session, only whether it's signed in and has an owner."""
        status = self.manager.box_status()
        if not session:
            return {"loggedIn": bool(status.get("loggedIn")), "owned": self.manager.owner() is not None}
        box = {name: status.get(name) for name in ("loggedIn", "email", "orgName", "subscriptionType")}
        if status.get("error"):
            box["unknown"] = True
        return box

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

    def _wants_fetch(self) -> bool:
        """Whether manager.js sent this: it updates the page itself, so a reload's flash would come later, out of place."""
        return self.headers.get("X-Opsbox-Fetch") == "1"

    def _no_content(self) -> None:
        """Done, and nothing to say."""
        self._headers(204, "text/plain")

    def _refuse(self, status: int, message: str) -> None:
        """Say why not: to manager.js as JSON, to a plain form as the page with an error on it."""
        if self._wants_fetch():
            self._json(status, {"error": message})
        else:
            self._page(status=status, error=message)

    def _redirect(self, flash: str | None = None, cookies: list[str] = ()) -> None:
        """A 303 back to the page, with a flash message id when there is one."""
        if self._wants_fetch() and not cookies:
            self._no_content()
            return
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
            "csrf": self.manager.sessions.csrf(session) if session else "",
            "presession": presession,
            "box": self._box(session),
            "rc": self.manager.rc.status() if session else {},
            "login": self._login(presession),
            "ssh": keys.describe_ssh() if session else {},
            "files": files.listing() if session else [],
            "env": keys.env_rows() if session else [],
            "browsers": self._browsers(session) if session else [],
            "flash": flash,
            "error": error,
        }
        self._send(status, self.manager.render(ctx).encode(), "text/html; charset=utf-8", cookies=set_cookies)

    def _browsers(self, session: str) -> list[dict]:
        """The signed-in browsers, with this one marked."""
        this = session_id(session)
        return [{**row, "current": row["id"] == this} for row in self.manager.sessions.list()]

    def _status(self, session: str) -> dict:
        """What manager.js needs to keep the page current without reloading it."""
        rc = self.manager.rc.status()
        symbol, word = STATE_LABELS.get(rc.get("state"), ("?", "Unknown"))
        shell = self.manager.shells.get(session_id(session))
        return {
            "box": self._box(session),
            "rc": {**rc, "symbol": symbol, "word": word, "detail": detail_line(rc)},
            "login": {"active": self.manager.login is not None},
            "shell": {"open": shell is not None and shell.running()},
        }

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
        raw = self.rfile.read(length)
        if len(raw) != length:
            self._text(400, b"That form arrived cut short.")
            return None
        try:
            fields = urllib.parse.parse_qs(raw.decode("utf-8", errors="replace"), keep_blank_values=True, max_num_fields=20)
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
            self._json(200, self._status(session))
        elif path == "/console":
            token = self._session()
            if token:
                self._stream(self.manager.console, token)
            else:
                self._json(401, {"error": "Sign in first."})
        elif path == "/shell/stream":
            self._shell_stream()
        else:
            self._text(404, b"Not found.")

    def _shell_stream(self) -> None:
        """This browser's shell, as server-sent events."""
        token = self._session()
        if not token:
            self._json(401, {"error": "Sign in first."})
            return
        shell = self.manager.shells.get(session_id(token))
        if shell is None:
            self._json(404, {"error": "This browser has no shell open."})
            return
        shell.viewer_joined()
        try:
            self._stream(shell.console, token, tell_end=True)
        finally:
            shell.viewer_left()

    def _stream(self, console, token: str, *, tell_end: bool = False) -> None:
        """Server-sent events: everything kept so far as one "reset" event, then each chunk as it comes."""
        backlog, q = console.attach()
        checked = time.monotonic()
        try:
            # No Content-Length: the stream lasts as long as the page is open,
            # and HTTP/1.0 ends it by closing the connection.
            self._send_stream_headers()
            self.wfile.write(b"event: reset\ndata: " + base64.b64encode(backlog) + b"\n\n")
            self.wfile.flush()
            while True:
                try:
                    chunk = q.get(timeout=15)
                except queue.Empty:
                    # Closed with this viewer's queue full, the None never arrived.
                    chunk = None if console.closed else b""
                if chunk is None:
                    if tell_end:
                        self.wfile.write(b"event: end\ndata: \n\n")
                        self.wfile.flush()
                    return
                if not console.subscribed(q):
                    # Dropped for falling behind; the page reconnects.
                    return
                # Before anything is written, so a signed-out browser's open
                # console gets nothing more (checked at most once a second).
                if time.monotonic() - checked >= 1:
                    if not self.manager.sessions.valid(token):
                        return
                    checked = time.monotonic()
                self.wfile.write(b"data: " + base64.b64encode(chunk) + b"\n\n" if chunk else b": keepalive\n\n")
                self.wfile.flush()
        except OSError:
            pass
        finally:
            console.unsubscribe(q)

    def _send_stream_headers(self) -> None:
        """Headers for the console's event stream: no length, and no proxy buffering."""
        self.send_response(200)
        for name, value in SECURITY_HEADERS.items():
            self.send_header(name, value)
        self.send_header("Content-Type", "text/event-stream")
        # nginx would otherwise hold the stream back until a buffer fills.
        self.send_header("X-Accel-Buffering", "no")
        self.end_headers()
        self._responded = True

    def _post(self, path: str) -> None:
        """Route a POST by exact path, after the CSRF check."""
        if path == "/files":
            self._upload()
            return
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
        try:
            self._action(path, form, session)
        except keys.Refused as refused:
            self._refuse(400, str(refused))

    def _action(self, path: str, form: dict[str, str], session: str) -> None:
        """A signed-in browser's change, by exact path; keys.Refused says why not."""
        manager = self.manager
        if path == "/rc/start":
            manager.rc.start()
            self._redirect("started")
        elif path == "/rc/stop":
            manager.rc.stop()
            self._redirect("stopped")
        elif path == "/rc/restart":
            if manager.rc.restart():
                self._redirect("restarted")
            else:
                self._refuse(400, "Remote Control isn't running, so there's nothing to restart.")
        elif path == "/rc/answer":
            answer = form.get("answer")
            if answer in ("yes", "no") and manager.rc.answer(answer == "yes", form.get("question", "")):
                self._redirect("answered" if answer == "yes" else "stopped")
            else:
                self._refuse(400, "Remote Control isn't asking anything.")
        elif path == "/console/input":
            self._typed(manager.rc.write, form, "Remote Control isn't running.")
        elif path == "/console/resize":
            size = self._size(form)
            if size:
                manager.rc.resize(*size)
                self._no_content()
        elif path == "/shell/open":
            size = self._size(form)
            if size:
                manager.shells.open(session_id(session), *size)
                self._no_content()
        elif path == "/shell/input":
            shell = manager.shells.get(session_id(session))
            self._typed(shell.write if shell else None, form, "This browser has no shell open.")
        elif path == "/shell/resize":
            size = self._size(form)
            shell = manager.shells.get(session_id(session))
            if size:
                if shell:
                    shell.resize(*size)
                self._no_content()
        elif path == "/ssh/generate":
            keys.generate_ssh()
            self._redirect("generated")
        elif path == "/ssh/remove":
            keys.remove_ssh()
            self._redirect("removed")
        elif path == "/files/remove":
            files.remove(form.get("path", ""))
            self._redirect("removed")
        elif path == "/env":
            keys.set_env(form.get("name", ""), form.get("value", ""), hidden=form.get("hidden") == "1")
            self._redirect("saved")
        elif path == "/env/remove":
            keys.remove_env(form.get("name", ""))
            self._redirect("removed")
        elif path == "/browsers/signout":
            this = session_id(session)
            target = form.get("id") or this
            # Sessions first: closing a shell takes a moment, and a session still
            # valid during it could open another.
            if target == this:
                manager.sessions.revoke(session)
            else:
                manager.sessions.revoke_id(target)
            manager.shells.close(target)
            if target == this:
                self._redirect("browser-signed-out", [_clear(SESSION_COOKIE)])
            else:
                self._redirect("other-signed-out")
        elif path == "/browsers/signout-all":
            manager.sessions.revoke_all()
            manager.shells.close_all()
            self._redirect("all-signed-out", [_clear(SESSION_COOKIE)])
        elif path == "/box/signout":
            manager.sign_box_out()
            self._redirect("box-signed-out", [_clear(SESSION_COOKIE)])
        else:
            self._text(404, b"Not found.")

    def _typed(self, write, form: dict[str, str], gone: str) -> None:
        """Hand keystrokes to a terminal."""
        data = form.get("data", "")
        if len(data) > MAX_INPUT:
            self._json(413, {"error": "That's too much to type at once."})
        elif write is not None and write(data.encode()):
            self._no_content()
        else:
            self._json(409, {"error": gone})

    def _size(self, form: dict[str, str]) -> tuple[int, int] | None:
        """A terminal's columns and rows from a form; None after refusing nonsense."""
        try:
            cols, rows = int(form.get("cols", "")), int(form.get("rows", ""))
        except ValueError:
            cols = rows = 0
        if 1 <= cols <= 1000 and 1 <= rows <= 1000:
            return cols, rows
        self._json(400, {"error": "That isn't a terminal size."})
        return None

    def _upload(self) -> None:
        """A file from the page's upload form. The session is checked before the body is even read."""
        session = self._session()
        if not session:
            self._text(403, b"Sign in first, then try again.")
            return
        try:
            length = int(self.headers.get("Content-Length", ""))
        except ValueError:
            self._text(411, b"Send a Content-Length.")
            return
        if length < 0 or length > MAX_UPLOAD:
            self._text(413, b"That's too much to send.")
            return
        body = self.rfile.read(length)
        fields = _multipart(self.headers.get("Content-Type", ""), body)
        if fields is None:
            self._text(400, b"That isn't an upload.")
            return
        csrf = fields.get("csrf", b"").decode("utf-8", errors="replace")
        if not self.manager.sessions.check_csrf(session, csrf):
            self._text(403, b"Sign in first, then try again.")
            return
        data = fields.get("file", b"")
        try:
            if not data:
                raise keys.Refused("That file is empty.")
            files.save(fields.get("path", b"").decode("utf-8", errors="replace"), data)
        except keys.Refused as refused:
            self._refuse(400, str(refused))
            return
        self._redirect("uploaded")

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


class Server(http.server.ThreadingHTTPServer):
    """A thread per connection, with a limit on how many one address can hold."""

    daemon_threads = True
    # socketserver's default of 5 resets connections in a burst, and a page load
    # is one: the page, its stylesheets, its scripts.
    request_queue_size = 128

    def __init__(self, *args, **kwargs) -> None:
        self._connections: dict[object, str] = {}
        self._connections_lock = threading.Lock()
        super().__init__(*args, **kwargs)

    def verify_request(self, request, client_address) -> bool:
        """Whether to take this connection: a False closes it, unanswered."""
        address = client_address[0]
        with self._connections_lock:
            held = sum(1 for other in self._connections.values() if other == address)
            if held >= MAX_CONNECTIONS_PER_CLIENT or len(self._connections) >= MAX_CONNECTIONS:
                return False
            self._connections[request] = address
        return True

    def shutdown_request(self, request) -> None:
        """A connection has ended, one way or the other."""
        with self._connections_lock:
            self._connections.pop(request, None)
        super().shutdown_request(request)


def serve(manager: Manager, host: str = "0.0.0.0", port: int = 8080) -> Server:
    """Create the server (the caller runs serve_forever)."""
    server = Server((host, port), Handler)
    server.manager = manager
    return server
