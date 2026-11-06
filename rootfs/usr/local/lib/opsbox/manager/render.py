"""Build the manager's one HTML page from a context dict.

The page is plain HTML forms, which work without JavaScript; manager.js only
adds the live terminal, Copy buttons and a refresh when the state changes.
Replace and the destructive confirms are <details>, so they need no script
and no second route. The Content-Security-Policy allows no inline script or
style, so there are no style= or on…= attributes here.

The house rules, from Stocktake: one line at a control and nothing for what
people already understand; every action is a button; one primary per form,
danger for what destroys something, and a confirm that says what survives;
status always has a word and a symbol, never colour alone.

Every value from the context goes through e(). Without a session the page
says nothing about the box beyond whether it's signed in, and it never shows a
key's value, only what the key authenticates as.
"""
from __future__ import annotations

import html
import time

def e(value: object) -> str:
    """Escape a value for HTML text or a quoted attribute."""
    return html.escape(str(value), quote=True)


# What each key's detail line shows, in order, from describe()'s fields.
DETAIL_FIELDS = {
    "kubernetes": ("context", "server", "user"),
    "talos": ("context", "endpoints"),
    "github": ("account",),
    "terraform": ("host",),
    "ssh": ("fingerprint",),
}


def csrf_field(ctx: dict) -> str:
    """The hidden field every form carries."""
    return '<input type="hidden" name="csrf" value="' + e(ctx["csrf"]) + '">'


def _badge(state: str) -> str:
    """Remote Control's state as a symbol and a word."""
    mapping = {
        "running": ("●", "Running"),
        "stopped": ("■", "Stopped"),
        "backing-off": ("◐", "Restarting soon"),
        "trust-asked": ("?", "Waiting for you"),
        "signed-out": ("○", "Not signed in"),
    }
    sym, word = mapping.get(state, ("?", "Unknown"))
    return '<span class="badge state-' + e(state) + '">' + sym + " " + e(word) + "</span>"


def _pagehead(ctx: dict) -> str:
    """The title, and with a session the box's account and Remote Control's state."""
    session = ctx["session"]
    box = ctx["box"]
    rc = ctx["rc"] if session else {}

    parts = ['<header class="pagehead">']
    parts.append("<h1>opsbox</h1>")

    if session:
        known = [e(box[name]) for name in ("email", "orgName", "subscriptionType") if box.get(name)]
        who = " \u00b7 ".join(known) if box.get("loggedIn") and known else "Not signed in to Claude"
        parts.append('<p class="who">' + who + "</p>")

    if session and rc:
        state = rc.get("state", "")
        parts.append(_badge(state))

    parts.append("</header>")
    return "\n".join(parts)


def _flash_and_error(ctx: dict) -> str:
    """What the last action did, or why it was refused."""
    parts = []
    if ctx.get("flash"):
        parts.append('<p class="flash" role="status">' + e(ctx["flash"]) + "</p>")
    if ctx.get("error"):
        parts.append('<p class="error" role="alert">' + e(ctx["error"]) + "</p>")
    return "\n".join(parts)


def _signin_section(ctx: dict) -> str:
    """The only section without a session: start, finish or cancel signing in to Claude."""
    box = ctx["box"]
    login = ctx["login"]
    parts = ['<section class="card signin">']
    parts.append("<h2>Sign in to Claude</h2>")

    if login["active"] and login["mine"]:
        url = login.get("url")
        if url and (url.startswith("https://claude.com/") or url.startswith("https://claude.ai/")):
            parts.append('<a class="button primary" href="' + e(url) + '" target="_blank" rel="noopener noreferrer">Open claude.ai</a>')
        else:
            parts.append('<p class="error" role="alert">Claude gave an unexpected sign-in link. Cancel and try again.</p>')

        parts.append('<form method="post" action="/signin/code">' + csrf_field(ctx))
        parts.append('<label for="code">Code from claude.ai</label>')
        parts.append('<input id="code" name="code" autocomplete="one-time-code" required>')
        parts.append('<button class="primary">Finish signing in</button>')
        parts.append("</form>")

        parts.append('<form method="post" action="/signin/cancel">' + csrf_field(ctx))
        parts.append('<button class="secondary">Cancel</button>')
        parts.append("</form>")

    elif login["active"] and not login["mine"]:
        parts.append('<p>Another browser is signing in. Starting here cancels it.</p>')
        parts.append('<form method="post" action="/signin/start">' + csrf_field(ctx))
        parts.append('<button class="primary">Sign in here</button>')
        parts.append("</form>")

    else:
        if box.get("loggedIn"):
            parts.append('<p>Sign in with the Claude account this box uses.</p>')
        else:
            parts.append('<p>Sign in to set up this box.</p>')
        parts.append('<form method="post" action="/signin/start">' + csrf_field(ctx))
        parts.append('<button class="primary">Sign in to Claude</button>')
        parts.append("</form>")

    parts.append("</section>")
    return "\n".join(parts)


def _remote_control(ctx: dict) -> str:
    """Start, stop and restart, and the trust question when it's asked."""
    rc = ctx["rc"]
    state = rc.get("state", "")
    restarts = rc.get("restarts", 0)
    last_exit = rc.get("last_exit")

    parts = ['<section class="card" id="remote-control"><h2>Remote Control</h2>']

    if restarts and restarts > 0:
        line = "Restarted " + e(str(restarts)) + " times"
        if last_exit is not None:
            line += " \u00b7 last exit " + e(str(last_exit))
        parts.append("<p>" + line + "</p>")

    parts.append('<div class="headactions">')

    start_class = "primary" if state in ("stopped", "signed-out") else "secondary"
    parts.append('<form method="post" action="/rc/start">' + csrf_field(ctx))
    parts.append('<button class="' + start_class + '">Start</button>')
    parts.append("</form>")

    parts.append('<form method="post" action="/rc/stop">' + csrf_field(ctx))
    parts.append('<button class="secondary">Stop</button>')
    parts.append("</form>")

    parts.append('<form method="post" action="/rc/restart">' + csrf_field(ctx))
    parts.append('<button class="secondary">Restart</button>')
    parts.append("</form>")

    parts.append("</div>")

    if state == "trust-asked":
        parts.append('<div class="callout">')
        parts.append("<p>Remote Control asks to trust " + e(rc.get("workdir") or "its working directory") + ". Everything under it runs with this box's keys.</p>")
        parts.append('<form method="post" action="/rc/trust">' + csrf_field(ctx))
        parts.append('<button class="primary">Trust</button>')
        parts.append("</form>")
        parts.append("</div>")

    parts.append("</section>")
    return "\n".join(parts)


def _console_section(_ctx: dict) -> str:
    """Where manager.js puts the live terminal."""
    parts = ['<section class="card" id="console"><h2>Console</h2>']
    parts.append('<div id="terminal" class="terminal" aria-label="Remote Control console"></div>')
    parts.append('<noscript><p>The live console needs JavaScript.</p></noscript>')
    parts.append("</section>")
    return "\n".join(parts)


def _keys_section(ctx: dict) -> str:
    """Each key: whether it's set, what it authenticates as, and Replace or Remove."""
    keys = ctx["keys"]
    parts = ['<section class="card" id="keys"><h2>Keys</h2>']
    parts.append('<ul class="keys">')

    for k in keys:
        label = k.get("label", "")
        key_id = k.get("id", "")
        is_set = k.get("set", False)
        hint = k.get("hint", "")

        parts.append('<li class="key">')
        parts.append('<div class="keyhead">')
        parts.append("<strong>" + e(label) + "</strong>")
        parts.append('<span class="badge ' + ("set" if is_set else "unset") + '">' + ("\u2713 Set" if is_set else "\u2013 Not set") + "</span>")
        parts.append("</div>")

        if "problem" in k:
            detail = "Can't read it: replace it."
        else:
            detail = " \u00b7 ".join(e(k[name]) for name in DETAIL_FIELDS.get(key_id, ()) if k.get(name))
        if detail:
            parts.append('<p class="detail">' + detail + "</p>")

        if not is_set and hint:
            parts.append('<p class="hint">' + e(hint) + "</p>")

        parts.append('<div class="keyactions">')

        if key_id == "ssh" and not is_set:
            parts.append('<form method="post" action="/keys/ssh/generate">' + csrf_field(ctx))
            parts.append('<button class="secondary small">Generate</button>')
            parts.append("</form>")

        if key_id == "ssh" and is_set and k.get("public"):
            parts.append('<button type="button" class="secondary small" data-copy="' + e(k["public"]) + '">Copy public key</button>')

        if key_id != "ssh":
            action_label = "Replace" if is_set else "Add"
            parts.append('<details class="replace">')
            parts.append('<summary class="button secondary small">' + e(action_label) + "</summary>")
            parts.append('<form method="post" action="/keys/' + e(key_id) + '">' + csrf_field(ctx))
            parts.append('<label for="value-' + e(key_id) + '">' + e(label) + "</label>")
            parts.append('<textarea id="value-' + e(key_id) + '" name="value" rows="6" required autocomplete="off" spellcheck="false"></textarea>')
            parts.append('<button class="primary">Save</button>')
            parts.append("</form>")
            parts.append("</details>")

        if is_set:
            parts.append('<details class="confirm">')
            parts.append('<summary class="button danger small">Remove</summary>')
            parts.append('<p>Remove the ' + e(label) + ' key? The other keys are kept.</p>')
            parts.append('<form method="post" action="/keys/' + e(key_id) + '/remove">' + csrf_field(ctx))
            parts.append('<button class="danger">Remove</button>')
            parts.append("</form>")
            parts.append("</details>")

        parts.append("</div>")
        parts.append("</li>")

    parts.append("</ul>")
    parts.append("</section>")
    return "\n".join(parts)


def _environment_section(ctx: dict) -> str:
    """The settings Remote Control starts with: each one's value, Remove, and a form to set one."""
    env = ctx["env"]
    env_allowed = ctx["env_allowed"]
    parts = ['<section class="card" id="environment"><h2>Environment</h2>']
    if env:
        parts.append('<table class="env">')
        parts.append("<thead><tr><th>Name</th><th>Value</th><th></th></tr></thead>")
        parts.append("<tbody>")

        for name, value in env.items():
            purpose = env_allowed.get(name, "")
            parts.append("<tr>")
            parts.append("<td>" + e(name))
            if purpose:
                parts.append("<small>" + e(purpose) + "</small>")
            parts.append("</td>")
            parts.append("<td>" + e(value) + "</td>")
            parts.append("<td>")
            parts.append('<form method="post" action="/env">' + csrf_field(ctx))
            parts.append('<input type="hidden" name="name" value="' + e(name) + '">')
            parts.append('<input type="hidden" name="value" value="">')
            parts.append('<button class="secondary small">Remove</button>')
            parts.append("</form>")
            parts.append("</td>")
            parts.append("</tr>")

        parts.append("</tbody></table>")

    parts.append('<form method="post" action="/env">' + csrf_field(ctx))
    parts.append('<label for="env-name">Setting</label>')
    parts.append('<select id="env-name" name="name">')
    for name in env_allowed:
        parts.append('<option value="' + e(name) + '">' + e(name) + "</option>")
    parts.append("</select>")
    parts.append('<label for="env-value">Value</label>')
    parts.append('<input id="env-value" name="value" required>')
    parts.append('<button class="primary">Save</button>')
    parts.append("</form>")

    parts.append('<p>Restart Remote Control to apply.</p>')
    parts.append("</section>")
    return "\n".join(parts)


def _browsers_section(ctx: dict) -> str:
    """The browsers with a session, and signing out this one or all of them."""
    browsers = ctx["browsers"]
    parts = ['<section class="card" id="browsers"><h2>Browsers</h2>']
    parts.append("<ul>")

    for b in browsers:
        label = b.get("label", "")
        created = b.get("created", 0)
        created_str = time.strftime("%Y-%m-%d", time.gmtime(created))
        parts.append("<li>" + e(label) + " signed in " + e(created_str) + "</li>")

    parts.append("</ul>")

    parts.append('<form method="post" action="/browsers/signout">' + csrf_field(ctx))
    parts.append('<button class="secondary">Sign out this browser</button>')
    parts.append("</form>")

    parts.append('<details class="confirm">')
    parts.append('<summary class="button danger">Sign out every browser</summary>')
    parts.append('<p>Sign out every browser? The box stays signed in to Claude.</p>')
    parts.append('<form method="post" action="/browsers/signout-all">' + csrf_field(ctx))
    parts.append('<button class="danger">Sign out every browser</button>')
    parts.append("</form>")
    parts.append("</details>")

    parts.append("</section>")
    return "\n".join(parts)


def _claude_section(ctx: dict) -> str:
    """Signing the box itself out of Claude."""
    parts = ['<section class="card" id="claude"><h2>Claude</h2>']
    parts.append('<details class="confirm">')
    parts.append('<summary class="button danger">Sign the box out</summary>')
    parts.append('<p>Sign the box out of Claude? Remote Control stops, and every browser is signed out. The keys are kept.</p>')
    parts.append('<form method="post" action="/box/signout">' + csrf_field(ctx))
    parts.append('<button class="danger">Sign the box out</button>')
    parts.append("</form>")
    parts.append("</details>")
    parts.append("</section>")
    return "\n".join(parts)


def page(ctx: dict) -> str:
    """The whole page for one request's context."""
    session = ctx["session"]
    login = ctx["login"]

    if session:
        rc = ctx["rc"]
        state = rc.get("state", "")
        login_val = 1 if login.get("active") else 0
        body_open = '<body data-state="' + e(state) + '" data-login="' + str(login_val) + '">'
    else:
        body_open = "<body>"

    sections = []
    sections.append(_pagehead(ctx))
    sections.append(_flash_and_error(ctx))

    if not session:
        sections.append(_signin_section(ctx))
    else:
        sections.append(_remote_control(ctx))
        sections.append(_console_section(ctx))
        sections.append(_keys_section(ctx))
        sections.append(_environment_section(ctx))
        sections.append(_browsers_section(ctx))
        sections.append(_claude_section(ctx))

    main_content = "\n".join(sections)

    if session:
        scripts = (
            '<script src="/static/xterm.js" defer></script>\n'
            '<script src="/static/xterm-addon-fit.js" defer></script>\n'
            '<script src="/static/manager.js" defer></script>'
        )
    else:
        scripts = '<script src="/static/manager.js" defer></script>'

    html_parts = [
        "<!doctype html>",
        '<html lang="en">',
        "<head>",
        '<meta charset="utf-8">',
        '<meta name="viewport" content="width=device-width, initial-scale=1">',
        "<title>opsbox</title>",
        '<link rel="stylesheet" href="/static/xterm.css">',
        '<link rel="stylesheet" href="/static/manager.css">',
        "</head>",
        body_open,
        "<main>",
        main_content,
        "</main>",
        scripts,
        "</body>",
        "</html>",
    ]

    return "\n".join(html_parts)
