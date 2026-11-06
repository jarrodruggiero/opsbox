"""The manager page at a desktop and a phone width, measured in a real browser.

Every other test reads the page as text, and a layout fault only exists at one
width: a long path or an SSH public key that pushes a phone's page sideways
renders perfectly at 1400px. Each state below goes through render.page into a
standalone file, and headless Chrome measures it at 1400 and 412px, with the
confirm popup shut and then open. The values are deliberately long, because real
ones are.

Skipped where there is no Chrome; CI's Ubuntu runner has one. The iframe and
the probe come from Stocktake's tests/test_visual.py.
"""

from __future__ import annotations

import base64
import http.server
import json
import os
import re
import shutil
import subprocess
import tempfile
import threading
import urllib.parse
from html import unescape
from pathlib import Path

import pytest

from opsbox.manager import render, web

ROOT = Path(__file__).resolve().parents[2]
STATIC = ROOT / "rootfs/usr/local/lib/opsbox/manager/static"
PROBE = (Path(__file__).parent / "visual_probe.js").read_text(encoding="utf-8")

# Names first, absolute paths second: a Linux runner has google-chrome on PATH
# and nothing under /Applications, and macOS the other way round.
CANDIDATES = (
    "google-chrome",
    "google-chrome-stable",
    "chromium",
    "chromium-browser",
    "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
    "/Applications/Chromium.app/Contents/MacOS/Chromium",
)


def find_chrome() -> str | None:
    """The first Chrome on this machine, or None."""
    for candidate in CANDIDATES:
        found = shutil.which(candidate)
        if found:
            return found
        if "/" in candidate and Path(candidate).is_file():
            return candidate
    return None


CHROME = find_chrome()
# CI sets OPSBOX_REQUIRE_BROWSER, so a runner without Chrome fails rather than
# skipping every layout test in silence.
if CHROME is None and os.environ.get("OPSBOX_REQUIRE_BROWSER"):
    raise RuntimeError("OPSBOX_REQUIRE_BROWSER is set but there is no Chrome")
pytestmark = pytest.mark.skipif(CHROME is None, reason="no headless Chrome on this machine")

# 1400 is a desktop; 412 is a Pixel-class phone in CSS pixels.
WIDTHS = {"desktop": 1400, "phone": 412}

# The page goes in an IFRAME of the wanted width, not a window of it: headless
# Chrome silently clamps --window-size to at least 500px, so a 412px window
# would really be measured at 500. An iframe gets its own viewport, so the
# media queries resolve to its width.
WRAPPER = """<!doctype html><html><body style="margin:0">
<iframe id="f" src="%s" style="width:%dpx;height:900px;border:0"></iframe>
<script>
var frame = document.getElementById("f"), tries = 0;
function collect() {
  var title = "";
  try { title = frame.contentDocument.title; } catch (e) { title = "BLOCKED " + e.message; }
  if (title.indexOf("VC") === 0 || title.indexOf("BLOCKED") === 0 || ++tries > 60) {
    document.title = title || "probe silent";
    return;
  }
  setTimeout(collect, 50);
}
frame.addEventListener("load", collect);
</script></body></html>
"""

LONG_EMAIL = "someone.with.a.long.name@an-organisation-with-a-long-domain.example.com"
LONG_HOST = "an-internal-domain-with-a-rather-long-name.example.internal"
USER_AGENT = (
    "Mozilla/5.0 (Linux; Android 16; Pixel 9 Pro XL) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/141.0.0.0 Mobile Safari/537.36"
)
RUNNING = {"state": "running", "restarts": 12, "last_exit": 1, "started_at": 1762400000, "pid": 4242, "workdir": "/home/opsbox/workspace", "question": None}


def context(**overrides: object) -> dict:
    """A signed-in page with something in every section and long values everywhere."""
    ctx: dict = {
        "session": True,
        "csrf": "c" * 64,
        "presession": "p" * 43,
        "flash": None,
        "error": None,
        "box": {"loggedIn": True, "email": LONG_EMAIL, "orgName": "An Organisation With A Long Name", "subscriptionType": "max"},
        "rc": RUNNING,
        "login": {"active": False, "mine": False, "url": None},
        "ssh": {"set": True, "public": "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAI" + "Qx7" * 14 + " opsbox", "fingerprint": "SHA256:" + "k3Y9" * 11},
        "files": [
            {"path": "~/.kube/config", "size": 5512, "uploaded": 1762400000},
            {"path": "~/.config/a-tool-with-a-long-name/an-even-longer-directory-name/credentials-for-the-tool.json", "size": 118, "uploaded": 1762400000},
            {"path": "~/averylongfilenamewithnothingtobreakatbecauseithasnoseparatorsinitatallandkeepsgoing", "size": 2048576, "uploaded": 1762300000},
        ],
        "env": [
            {"name": "DOCKER_HOST", "hidden": False, "value": "ssh://dockerbuild@buildhost.averylonginternaldomainnamewithnothingtobreakat.example:2222"},
            {"name": "GH_TOKEN", "hidden": True, "value": None},
            {"name": "NO_PROXY", "hidden": False, "value": f"localhost,127.0.0.1,.home,.{LONG_HOST},10.0.0.0/8"},
            {"name": "A_SETTING_WITH_A_VERY_LONG_NAME_THAT_HAS_NOWHERE_TO_BREAK_AT_ALL_BECAUSE_IT_IS_ALL_ONE_WORD", "hidden": False, "value": "x"},
        ],
        "browsers": [
            {"id": "a" * 16, "label": USER_AGENT, "created": 1762400000, "expires": 1765000000, "current": True},
            {"id": "b" * 16, "label": USER_AGENT.replace("Linux; Android 16; Pixel 9 Pro XL", "Macintosh; Intel Mac OS X 10_15_7"), "created": 1762300000, "expires": 1764900000, "current": False},
        ],
    }
    ctx.update(overrides)
    return ctx


SIGNED_OUT = {"session": False, "csrf": "", "rc": {}, "ssh": {}, "files": [], "env": [], "browsers": [], "box": {"loggedIn": True, "owned": True}}
SIGNING_IN = {"active": True, "mine": True, "url": "https://claude.com/cai/oauth/authorize?code=true&client_id=" + "f" * 36 + "&state=" + "s" * 43}
STATES = {
    "signed-out": context(**SIGNED_OUT),
    "signing-in": context(**SIGNED_OUT | {"login": SIGNING_IN}),
    "another-browser": context(**SIGNED_OUT | {"login": {"active": True, "mine": False, "url": None}}),
    "refused": context(**SIGNED_OUT, error="That's a different Claude account from the one this box uses."),
    "running": context(flash="Saved."),
    "asking": context(rc=RUNNING | {"state": "asking", "question": "Trust /home/opsbox/workspace-with-a-rather-long-name/and-a-long-subdirectory-under-it? [y/N]"}),
    "stopped": context(rc=RUNNING | {"state": "stopped", "restarts": 0, "last_exit": None}),
    "nothing-set": context(ssh={"set": False}, files=[], env=[]),
    "box-signed-out": context(box={"loggedIn": False}, rc=RUNNING | {"state": "signed-out"}),
    "action-refused": context(error="The path must be under the box's home directory (~), and this message is long enough to wrap at a phone's width."),
}


def standalone(ctx: dict) -> str:
    """The rendered page as a file:// document: real styles, no scripts, and the probe."""
    html = render.page(ctx)
    # The page's own scripts would only fail to reach the server here; layout
    # is the CSS's job. test_the_page_in_a_real_browser runs them.
    html = re.sub(r"<script\b[^>]*></script>", "", html)
    html = html.replace('"/static/', '"' + STATIC.as_uri() + "/")
    return html.replace("</body>", f"<script>{PROBE}</script></body>")


def probe(html: str, width: int) -> list[str]:
    """Measure one page at one width; what the probe found wrong, in words."""
    with tempfile.TemporaryDirectory() as tmp:
        page = Path(tmp) / "page.html"
        page.write_text(html, encoding="utf-8")
        wrapper = Path(tmp) / "wrapper.html"
        wrapper.write_text(WRAPPER % (page.name, width), encoding="utf-8")
        out = subprocess.run(
            [CHROME, "--headless", "--disable-gpu", "--dump-dom",
             # Two file:// documents are different origins without it, and
             # the wrapper couldn't read the page's title.
             "--allow-file-access-from-files",
             # Chrome refuses to start where it can't get a sandbox (containers,
             # some runners).
             "--no-sandbox",
             f"--window-size={max(width + 80, 620)},960",
             "--virtual-time-budget=6000",
             wrapper.as_uri()],
            capture_output=True, text=True, timeout=90,
        ).stdout
    marker = out.find("VC[")
    if marker < 0:
        return ["the probe did not run"]
    # --dump-dom escapes the title's text as HTML.
    return json.loads(unescape(out[marker + 2:out.find("</title>", marker)]))


@pytest.mark.parametrize("width", sorted(WIDTHS))
@pytest.mark.parametrize("state", sorted(STATES))
def test_the_page_fits_and_lines_up(state: str, width: str) -> None:
    """No sideways scrolling, nothing past an edge, no text outside its box, and buttons in line."""
    found = probe(standalone(STATES[state]), WIDTHS[width])
    assert not found, f"{state} at {width} ({WIDTHS[width]}px):\n  " + "\n  ".join(found[:8])


def test_the_probe_can_see_a_page_too_wide() -> None:
    """A page with a 2000px-wide element must be reported, or every pass above means nothing."""
    html = standalone(context()).replace("</main>", '<div class="detail">' + "x" * 400 + "</div></main>")
    html = html.replace("</head>", "<style>.detail{overflow-wrap:normal !important;white-space:nowrap}</style></head>")
    assert probe(html, WIDTHS["phone"]), "the probe found nothing wrong with an over-wide page"


def test_the_probe_can_see_buttons_out_of_line() -> None:
    """A button pushed down, or a row's actions short of the edge, must be reported."""
    pushed = standalone(context()).replace("</head>", "<style>.row > .actions > form button{margin-top:9px}</style></head>")
    assert any("out of line" in fault for fault in probe(pushed, WIDTHS["desktop"]))
    short = standalone(context()).replace("</head>", "<style>.row > .actions{margin-right:40px}</style></head>")
    assert any("right edge" in fault for fault in probe(short, WIDTHS["desktop"]))


UI = ROOT / "ui/node_modules"
VENDORED = {
    "xterm.js": UI / "@xterm/xterm/lib/xterm.js",
    "xterm.css": UI / "@xterm/xterm/css/xterm.css",
    "xterm-addon-fit.js": UI / "@xterm/addon-fit/lib/addon-fit.js",
}


def in_a_real_browser(ctx: dict) -> tuple[dict, list[tuple[str, str, dict[str, str]]], set[str]]:
    """Serve the real page with the real headers and scripts, let interact_probe.js drive it.

    Returns what the probe saw, every request that reached the server as
    (method, path, form fields), and the paths manager.js sent as a fetch.
    """
    missing = [name for name, path in VENDORED.items() if not path.is_file()]
    if missing:
        if os.environ.get("OPSBOX_REQUIRE_BROWSER"):
            pytest.fail(f"the terminal isn't vendored ({', '.join(missing)}): run npm ci --ignore-scripts in ui/")
        pytest.skip("the terminal isn't vendored into ui/node_modules")
    files = {name: path.read_bytes() for name, path in VENDORED.items()}
    for name in ("manager.css", "manager.js", "icon.svg"):
        files[name] = (STATIC / name).read_bytes()
    files["interact_probe.js"] = (Path(__file__).parent / "interact_probe.js").read_bytes()
    page = render.page(ctx).replace("</body>", '<script src="/static/interact_probe.js"></script></body>').encode()
    # With a terminal query in it, as old output can have: xterm answers those as
    # if typed, and replaying this after a reload must not type anything.
    greeting = base64.b64encode(b"\x1b[32mhello from the console\x1b[0m\x1b[c\x1b[6n\r\n").decode()
    reset = f"retry: 600000\nevent: reset\ndata: {greeting}\n\n".encode()
    streams = {"/console": 0, "/shell/stream": 0}
    status = {
        "box": {"loggedIn": True},
        "rc": {"state": "asking", "symbol": "?", "word": "Waiting for you", "detail": "", "question": "Enable Remote Control? (y/n)"},
        "login": {"active": False},
        "shell": {"open": True},
    }
    seen: list[tuple[str, str, dict[str, str]]] = []
    fetched: set[str] = set()

    class Serve(http.server.BaseHTTPRequestHandler):
        def reply(self, code: int, body: bytes, kind: str) -> None:
            self.send_response(code)
            for header, value in web.SECURITY_HEADERS.items():
                self.send_header(header, value)
            self.send_header("Content-Type", kind)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self) -> None:
            seen.append(("GET", self.path, {}))
            name = self.path.removeprefix("/static/")
            if self.path == "/":
                self.reply(200, page, "text/html; charset=utf-8")
            elif self.path.startswith("/static/") and name in files:
                kinds = {".css": "text/css", ".svg": "image/svg+xml"}
                self.reply(200, files[name], kinds.get(Path(name).suffix, "text/javascript"))
            elif self.path in streams:
                streams[self.path] += 1
                if self.path == "/console" and streams[self.path] == 2:
                    # A proxy during a restart, say: EventSource doesn't retry this.
                    self.reply(503, b"", "text/plain")
                elif self.path == "/shell/stream" and streams[self.path] == 2:
                    self.reply(200, reset + b"event: end\ndata: \n\n", "text/event-stream")
                else:
                    self.reply(200, reset, "text/event-stream")
            else:
                self.reply(200, json.dumps(status).encode(), "application/json")

        def do_POST(self) -> None:
            body = self.rfile.read(int(self.headers.get("Content-Length", "0"))).decode()
            fields = {name: values[0] for name, values in urllib.parse.parse_qs(body, keep_blank_values=True).items()}
            seen.append(("POST", self.path, fields))
            if self.headers.get("X-Opsbox-Fetch") == "1":
                fetched.add(self.path)
            self.reply(204, b"", "text/plain")

        def log_message(self, *args: object) -> None:
            pass

    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Serve)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        out = subprocess.run(
            [CHROME, "--headless", "--disable-gpu", "--dump-dom", "--no-sandbox", "--window-size=1200,900",
             "--virtual-time-budget=20000", f"http://127.0.0.1:{server.server_address[1]}/"],
            capture_output=True, text=True, timeout=90,
        ).stdout
    finally:
        server.shutdown()
        server.server_close()
    marker = out.find("VC{")
    assert marker >= 0, f"the probe did not finish; the server saw {[entry[:2] for entry in seen]}"
    return json.loads(unescape(out[marker + 2:out.find("</title>", marker)])), seen, fetched


def test_the_page_in_a_real_browser() -> None:
    """The real scripts, under the real Content-Security-Policy, doing what a person does.

    The layout tests strip the scripts, so none of this shows there: the first
    CSP blocked the <style> elements xterm.js writes, and the console lost its
    colours and its columns. And "the console takes input" is only true if
    keystrokes really leave the browser, with the token, in order.
    """
    report, seen, fetched = in_a_real_browser(context())
    posts = [(path, fields) for method, path, fields in seen if method == "POST"]
    paths = [path for path, _fields in posts]
    token = "c" * 64

    assert report["violations"] == [], report["violations"]
    # Styled: monospace, and not the black a blocked theme leaves on a near-black ground.
    assert "mono" in report["font"].lower() or "courier" in report["font"].lower(), report["font"]
    assert report["color"] != "rgb(0, 0, 0)", report["color"]

    # The confirm is a popup, in the form's words, and Cancel sends nothing.
    assert report["dialogOpen"] is True
    assert report["dialogText"].startswith("Remove the SSH key?")
    assert report["dialogOk"] == "Remove"
    assert report["closedByCancel"] is True
    assert "/ssh/remove" not in paths

    # Typing in the console reaches Remote Control.
    # Exactly that: answers to the terminal queries in the replayed output aren't typing.
    assert [fields for path, fields in posts if path == "/console/input"] == [{"csrf": token, "data": "y"}]
    # The terminal told the server its size.
    resized = [fields for path, fields in posts if path == "/console/resize"]
    assert resized and int(resized[0]["cols"]) > 20 and resized[0]["csrf"] == token

    # The poll brought the question without a reload, and Yes went by fetch.
    assert report["question"] == "Enable Remote Control? (y/n)"
    assert report["word"] == "Waiting for you"
    assert "state-asking" in report["stateClass"]
    assert ("/rc/answer", {"csrf": token, "answer": "yes", "question": "Enable Remote Control? (y/n)"}) in posts
    assert "/rc/answer" in fetched, "a button that should leave the terminal alone sent a plain form"
    assert [path for method, path, _f in seen if method == "GET" and path == "/"] == ["/"], "the page was reloaded"

    # The Shell tab opens this browser's shell, streams it, and takes typing too.
    assert report["rcHidden"] is True and report["shellHidden"] is False and report["shellSelected"] == "true"
    assert report["actionsHidden"] is True
    opened = [fields for path, fields in posts if path == "/shell/open"]
    assert int(opened[0]["cols"]) > 20 and opened[0]["csrf"] == token
    # Typed, then away and back (the server says it has ended), a key that goes
    # nowhere, Enter for a new shell, and typing in that one.
    assert [fields["data"] for path, fields in posts if path == "/shell/input"] == ["z", "w"]
    assert len(opened) == 2
    assert seen.count(("GET", "/shell/stream", {})) == 3

    # A refused stream comes back with the next status: open, refused, open again.
    assert seen.count(("GET", "/console", {})) == 3

    # Confirmed, the form goes: the popup's own button is what sends it.
    assert any(path == "/env/remove" and fields.get("name") == "DOCKER_HOST" for path, fields in posts)


def test_pasting_the_code_finishes_signing_in() -> None:
    """The code can't be fetched automatically, so a paste is the last step: nothing else to click."""
    report, seen, _fetched = in_a_real_browser(STATES["signing-in"])
    assert report["violations"] == [] and report["pasted"] is True
    assert ("POST", "/signin/code", {"csrf": "p" * 43, "code": "pasted-code-123"}) in seen
    # It says it's working: with no button pressed, nothing else would.
    assert report["finishText"] == "Signing in\u2026" and report["finishDisabled"] is True
