"""The manager page at a desktop and a phone width, measured in a real browser.

Every other test reads the page as text, and a layout fault only exists at one
width: a key's server name or an SSH public key that pushes a phone's page
sideways renders perfectly at 1400px. Each state below goes through
render.page into a standalone file, and headless Chrome measures it at 1400 and
412px, with every <details> shut and then open. The values are deliberately
long, because real ones are.

Skipped where there is no Chrome; CI's Ubuntu runner has one. The iframe and
the probe come from Stocktake's tests/test_visual.py.
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
import tempfile
from html import unescape
from pathlib import Path

import pytest

from opsbox.manager import render

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


def context(**overrides: object) -> dict:
    """A signed-in page with every key set and long values everywhere."""
    ctx: dict = {
        "session": True,
        "csrf": "c" * 43,
        "flash": None,
        "error": None,
        "box": {"loggedIn": True, "email": LONG_EMAIL, "orgName": "An Organisation With A Long Name", "subscriptionType": "max"},
        "rc": {"state": "running", "restarts": 12, "last_exit": 1, "started_at": 1762400000, "pid": 4242},
        "login": {"active": False, "mine": False, "url": None},
        "keys": [
            {"id": "kubernetes", "label": "Kubernetes", "hint": "", "set": True, "updated": 1762400000,
             "context": "admin@home-cluster-with-a-long-name", "server": f"https://kubernetes.{LONG_HOST}:6443", "user": "opsbox-read-only"},
            {"id": "talos", "label": "Talos", "hint": "", "set": True, "updated": 1762400000,
             "context": "home", "endpoints": "10.1.5.10, 10.1.5.11, 10.1.5.12, 10.1.5.13, 10.1.5.14"},
            {"id": "github", "label": "GitHub", "hint": "A fine-grained personal access token.", "set": True, "account": "an-account-with-a-long-name"},
            {"id": "terraform", "label": "Terraform registry", "hint": "An app.terraform.io API token.", "set": True, "updated": 1762400000, "host": "app.terraform.io"},
            {"id": "local-llm", "label": "Local LLM", "hint": "", "set": True, "updated": 1762400000},
            {"id": "ssh", "label": "SSH", "hint": "", "set": True, "updated": 1762400000,
             "public": "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAI" + "Qx7" * 14 + " opsbox",
             "fingerprint": "SHA256:" + "k3Y9" * 11},
        ],
        "env": {
            "HTTPS_PROXY": f"http://proxy.{LONG_HOST}:3128",
            "NO_PROXY": f"localhost,127.0.0.1,.home,.{LONG_HOST},10.0.0.0/8",
            "TZ": "Australia/Melbourne",
            # No hyphen, space or slash for a line to break at.
            "DOCKER_HOST": "ssh://dockerbuild@buildhost.averylonginternaldomainnamewithnothingtobreakat.example:2222",
        },
        "env_allowed": {
            "CLAUDE_REMOTE_CONTROL_SESSION_NAME_PREFIX": "The prefix of each session's name in the Claude app.",
            "DOCKER_HOST": "Where the docker CLI builds and runs containers.",
            "TZ": "The timezone for logs and local times.",
            "GIT_AUTHOR_NAME": "The name on commits.",
            "GIT_AUTHOR_EMAIL": "The email on commits.",
            "HTTPS_PROXY": "A proxy for outbound HTTPS.",
            "NO_PROXY": "Hosts that skip the proxy.",
        },
        "browsers": [
            {"label": USER_AGENT, "created": 1762400000, "expires": 1765000000},
            {"label": USER_AGENT.replace("Android 16; Pixel 9 Pro XL", "Macintosh; Intel Mac OS X 10_15_7"), "created": 1762300000, "expires": 1764900000},
        ],
    }
    ctx.update(overrides)
    return ctx


def unset(key: dict) -> dict:
    """The same key, not set."""
    return {"id": key["id"], "label": key["label"], "hint": key["hint"], "set": False}


SIGNED_OUT = {"session": False, "keys": [], "env": {}, "browsers": []}
STATES = {
    "signed-out": context(**SIGNED_OUT, box={"loggedIn": False}),
    "signing-in": context(**SIGNED_OUT, login={"active": True, "mine": True,
                          "url": "https://claude.com/cai/oauth/authorize?code=true&client_id=" + "f" * 36 + "&state=" + "s" * 43}),
    "another-browser": context(**SIGNED_OUT, login={"active": True, "mine": False, "url": None}),
    "refused": context(**SIGNED_OUT, error="That's a different Claude account from the one this box uses."),
    "running": context(flash="Saved."),
    "trust-asked": context(rc={"state": "trust-asked", "restarts": 0, "last_exit": None, "started_at": 1762400000, "pid": 4242}),
    "nothing-set": context(box={"loggedIn": False}, keys=[unset(k) for k in context()["keys"]], env={},
                           rc={"state": "signed-out", "restarts": 0, "last_exit": None, "started_at": None, "pid": None}),
    "key-refused": context(error="GitHub refused that token: HTTP 401: Bad credentials (https://api.github.com/graphql) — try again"),
}


def standalone(ctx: dict) -> str:
    """The rendered page as a file:// document: real styles, no scripts, and the probe."""
    html = render.page(ctx)
    # The page's own scripts would only fail to reach /console here; layout
    # is the CSS's job.
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
def test_the_page_fits(state: str, width: str) -> None:
    """No sideways scrolling, nothing past the edge, no text outside its box."""
    found = probe(standalone(STATES[state]), WIDTHS[width])
    assert not found, f"{state} at {width} ({WIDTHS[width]}px):\n  " + "\n  ".join(found[:8])


def test_the_probe_can_fail() -> None:
    """A page with a 2000px-wide element must be reported, or every pass above means nothing."""
    html = standalone(context()).replace("</main>", '<div class="detail">' + "x" * 400 + "</div></main>")
    html = html.replace("</head>", "<style>.detail{overflow-wrap:normal !important;white-space:nowrap}</style></head>")
    assert probe(html, WIDTHS["phone"]), "the probe found nothing wrong with an over-wide page"
