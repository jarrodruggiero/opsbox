"""Build the manager's one HTML page from a context dict.

The page is plain HTML forms, which work without JavaScript; manager.js adds the
terminals, keeps the state current without reloading, and asks before anything
destructive. The Content-Security-Policy allows no inline script or style, so
there are no style= or on…= attributes here.

The house rules, from Stocktake: one line at a control and nothing for what
people already understand; every action is a button; one primary per form,
danger for what destroys something, and a confirm that says what survives;
status always has a word and a symbol, never colour alone.

Every value from the context goes through e(). Without a session the page says
nothing about the box beyond whether it's signed in. It never shows a hidden
setting's value or a file's contents.
"""
from __future__ import annotations

import html
import time

from .supervisor import STATE_LABELS, detail_line

# The mark: an isometric cube on the suite's 24 grid, stroked at 2 with round
# joins, and exactly one element in the accent, as Stocktake's and the rest are.
MARK_PATHS = (
    '<path d="M3.77 7.25v9.5L12 21.5l8.23-4.75v-9.5"/><path d="M12 12v9.5"/>'
    '<path class="top" d="M12 2.5l8.23 4.75L12 12 3.77 7.25z"/>'
)
MARK = (
    '<svg class="mark" viewBox="0 0 24 24" fill="none" stroke-width="2" stroke-linecap="round"'
    f' stroke-linejoin="round" aria-hidden="true">{MARK_PATHS}</svg>'
)

# Which of Remote Control's buttons make sense in each state. manager.js applies
# the same list as the state changes.
ACTION_STATES = {
    "start": ("stopped",),
    "stop": ("running", "asking", "backing-off", "signed-out"),
    "restart": ("running", "asking", "backing-off"),
}


def e(value: object) -> str:
    """Escape a value for HTML text or a quoted attribute."""
    return html.escape(str(value), quote=True)


def browser_name(user_agent: str) -> str:
    """A User-Agent as a person would say it: "Chrome on Windows"."""
    for token, name in (("Edg/", "Edge"), ("OPR/", "Opera"), ("Firefox/", "Firefox"), ("Chrome/", "Chrome"), ("Safari/", "Safari")):
        if token in user_agent:
            browser = name
            break
    else:
        browser = "A browser"
    for token, name in (("iPhone", "iPhone"), ("iPad", "iPad"), ("Android", "Android"), ("Windows", "Windows"), ("Mac OS X", "macOS"), ("Linux", "Linux")):
        if token in user_agent:
            return f"{browser} on {name}"
    return browser


def size_text(size: int) -> str:
    """A file's size for a person to read."""
    if size < 1024:
        return f"{size} B"
    if size < 1048576:
        return f"{size / 1024:.1f} KB"
    return f"{size / 1048576:.1f} MB"


def date_text(when: float) -> str:
    """A date for a person to read: 6 Nov 2026."""
    local = time.localtime(when)
    return f"{local.tm_mday} {time.strftime('%b %Y', local)}"


def _field(value: str, name: str = "csrf") -> str:
    """A hidden form field."""
    return f'<input type="hidden" name="{e(name)}" value="{e(value)}">'


def _form(ctx: dict, action: str, label: str, *, css: str = "secondary", confirm: str | None = None,
          fields: dict[str, str] | None = None, fetch: bool = False, attrs: str = "") -> str:
    """A form that's only a button: every action on the page that takes no typing."""
    extra = "".join(_field(value, name) for name, value in (fields or {}).items())
    asks = f' data-confirm="{e(confirm)}"' if confirm else ""
    live = " data-fetch" if fetch else ""
    return (
        f'<form method="post" action="{e(action)}"{asks}{live}{attrs}>{_field(ctx["csrf"])}{extra}'
        f'<button class="{css}">{e(label)}</button></form>'
    )


def _status(rc: dict) -> str:
    """Remote Control's state as a symbol and a word."""
    state = rc.get("state", "")
    symbol, word = STATE_LABELS.get(state, ("?", "Unknown"))
    return (
        f'<span id="rc-status" class="status state-{e(state)}">'
        f'<span class="symbol" aria-hidden="true">{symbol}</span><span class="word">{e(word)}</span></span>'
    )


def _topbar(ctx: dict) -> str:
    """The bar along the top: the name, Remote Control's state and who the box is signed in as."""
    email = ctx["box"].get("email")
    who = f'<span class="who">{e(email)}</span>' if email else ""
    return (
        f'<header class="topbar"><a class="brand" href="/">{MARK}<span>opsbox</span></a>'
        f'<span class="spacer"></span>{who}{_status(ctx["rc"])}</header>'
    )


def _notices(ctx: dict) -> str:
    """What the last action did, or why it was refused."""
    parts = ['<div id="notice" class="notice" role="status" hidden></div>']
    if ctx.get("flash"):
        parts = [f'<div id="notice" class="notice" role="status" data-fades>{e(ctx["flash"])}</div>']
    if ctx.get("error"):
        parts.append(f'<div class="notice bad" role="alert">{e(ctx["error"])}</div>')
    return "".join(parts)


def _signin(ctx: dict) -> str:
    """Start, finish or cancel signing in to Claude: the whole page without a session."""
    login = ctx["login"]
    box = ctx["box"]
    token = ctx["presession"]
    heading = "<h1>Sign in to Claude</h1>" if not ctx["session"] else "<h2>Sign in to Claude</h2>"
    parts = [f'<section class="panel signin">{heading}']
    if login["active"] and login["mine"]:
        url = login.get("url") or ""
        if url.startswith(("https://claude.com/", "https://claude.ai/")):
            opener = f'<a class="button secondary" href="{e(url)}" target="_blank" rel="noopener noreferrer">Open claude.ai</a>'
        else:
            opener = '<p class="notice bad" role="alert">Claude gave an unexpected sign-in link. Cancel and try again.</p>'
        parts.append(
            f'<ol class="steps"><li>{opener}</li><li>'
            f'<form method="post" action="/signin/code" id="code-form">{_field(token)}'
            '<label for="code">Code from claude.ai</label>'
            '<input id="code" name="code" autocomplete="one-time-code" spellcheck="false" autocapitalize="none" required autofocus>'
            '<div class="formactions"><button class="primary">Finish signing in</button></div></form></li></ol>'
            f'<form method="post" action="/signin/cancel" class="aside">{_field(token)}<button class="text">Cancel</button></form>'
        )
    elif login["active"]:
        parts.append(
            '<p class="sub">Another browser is signing in. Starting here cancels it.</p>'
            f'<form method="post" action="/signin/start">{_field(token)}<div class="formactions"><button class="primary">Sign in here</button></div></form>'
        )
    else:
        line = "Sign in with the Claude account this box uses." if box.get("loggedIn") or box.get("owned") else "Sign in to set up this box."
        parts.append(
            f'<p class="sub">{line}</p>'
            f'<form method="post" action="/signin/start">{_field(token)}<div class="formactions"><button class="primary">Sign in to Claude</button></div></form>'
        )
    parts.append("</section>")
    return "".join(parts)


def _console(ctx: dict) -> str:
    """Remote Control's buttons and any question it's asking, and the terminals manager.js puts here."""
    rc = ctx["rc"]
    state = rc.get("state", "")
    actions = []
    for name, label, css in (("start", "Start", "primary"), ("stop", "Stop", "secondary"), ("restart", "Restart", "secondary")):
        states = ACTION_STATES[name]
        hidden = "" if state in states else " hidden"
        actions.append(_form(ctx, f"/rc/{name}", label, css=css, fetch=True, attrs=f' data-show="{" ".join(states)}"{hidden}'))
    question = rc.get("question")
    return (
        '<section class="panel" id="console"><div class="panelhead"><div class="headgroup"><h2>Console</h2>'
        '<div class="tabs" role="tablist" aria-label="Console">'
        '<button type="button" role="tab" class="tab" id="tab-rc" aria-selected="true" aria-controls="pane-rc">Remote Control</button>'
        '<button type="button" role="tab" class="tab" id="tab-shell" aria-selected="false" aria-controls="pane-shell">Shell</button>'
        f'</div><p class="sub" id="rc-detail">{e(detail_line(rc))}</p></div>'
        f'<div class="actions" id="rc-actions">{"".join(actions)}</div></div>'
        f'<div class="callout" id="rc-question"{"" if question else " hidden"}>'
        f'<p id="rc-question-text">{e(question or "")}</p><div class="actions">'
        f'{_form(ctx, "/rc/answer", "Yes", css="primary", fetch=True, fields={"answer": "yes", "question": question or ""})}'
        f'{_form(ctx, "/rc/answer", "No", fetch=True, fields={"answer": "no", "question": question or ""})}'
        '</div></div><div class="termframe">'
        '<div class="terminal" id="pane-rc" role="tabpanel" aria-labelledby="tab-rc"></div>'
        '<div class="terminal" id="pane-shell" role="tabpanel" aria-labelledby="tab-shell" hidden></div>'
        '</div><noscript><p class="sub">The console needs JavaScript.</p></noscript></section>'
    )


def _files(ctx: dict) -> str:
    """The config files uploaded to the box, and the form to upload another."""
    rows = "".join(
        f'<li class="row"><div class="rowmain"><div class="rowname">{e(f["path"])}</div>'
        f'<div class="rowmeta">{e(size_text(f["size"]))} · {e(date_text(f["uploaded"]))}</div></div>'
        f'<div class="actions"><button type="button" class="secondary small" data-replace="{e(f["path"])}">Replace</button>'
        f'{_form(ctx, "/files/remove", "Remove", css="danger small", fields={"path": f["path"]}, confirm=f"Remove {f["path"]}? The other files are kept.")}'
        "</div></li>"
        for f in ctx["files"]
    )
    listing = f'<ul class="rows">{rows}</ul>' if rows else '<p class="empty">No files yet.</p>'
    return (
        '<section class="panel" id="files"><div class="panelhead"><h2>Config files</h2></div>'
        f'{listing}<form class="stacked" id="upload-form" method="post" action="/files" enctype="multipart/form-data">{_field(ctx["csrf"])}'
        '<div class="field"><label for="file-input">File</label><input id="file-input" type="file" name="file" required></div>'
        '<div class="field"><label for="file-path">Path on the box</label>'
        '<input id="file-path" name="path" placeholder="~/.kube/config" spellcheck="false" autocapitalize="none" autocomplete="off" required></div>'
        '<div class="formactions"><button class="primary">Upload</button></div></form></section>'
    )


def _environment(ctx: dict) -> str:
    """The settings Remote Control and shells start with, and the form to set one."""
    rows = "".join(
        f'<li class="row"><div class="rowmain"><div class="rowname">{e(row["name"])}</div>'
        + (
            '<div class="rowmeta">Hidden</div>' if row["hidden"]
            else f'<div class="rowmeta value">{e(row["value"])}</div>'
        )
        + f'</div><div class="actions">{_form(ctx, "/env/remove", "Remove", css="danger small", fields={"name": row["name"]}, confirm=f"Remove {row["name"]}? The other settings are kept.")}'
        "</div></li>"
        for row in ctx["env"]
    )
    listing = f'<ul class="rows">{rows}</ul>' if rows else '<p class="empty">No settings yet.</p>'
    return (
        '<section class="panel" id="environment"><div class="panelhead"><div><h2>Environment</h2>'
        '<p class="sub">Restart Remote Control to apply.</p></div></div>'
        f'{listing}<form class="stacked" method="post" action="/env" id="env-form">{_field(ctx["csrf"])}'
        '<div class="fieldrow"><div class="field"><label for="env-name">Name</label>'
        '<input id="env-name" name="name" placeholder="DOCKER_HOST" spellcheck="false" autocapitalize="none" autocomplete="off" required></div>'
        '<div class="field"><label for="env-value">Value</label>'
        '<input id="env-value" name="value" spellcheck="false" autocapitalize="none" autocomplete="off" required></div></div>'
        '<div class="formactions spread"><label class="check"><input id="env-hidden" type="checkbox" name="hidden" value="1"> Hide the value</label>'
        '<button class="primary">Save</button></div></form></section>'
    )


def _ssh(ctx: dict) -> str:
    """The box's SSH key: generate it here, and copy its public half to where it's needed."""
    ssh = ctx["ssh"]
    if not ssh.get("set"):
        return (
            '<section class="panel" id="ssh"><div class="panelhead"><h2>SSH key</h2>'
            f'<div class="actions">{_form(ctx, "/ssh/generate", "Generate", css="primary small")}</div></div>'
            '<p class="empty">No key yet.</p></section>'
        )
    public = ssh.get("public", "")
    copy = f'<button type="button" class="secondary small" data-copy="{e(public)}">Copy public key</button>' if public else ""
    fingerprint = f'<div class="rowmeta value">{e(ssh["fingerprint"])}</div>' if ssh.get("fingerprint") else ""
    shown = f'<code class="block">{e(public)}</code>' if public else ""
    return (
        '<section class="panel" id="ssh"><div class="panelhead"><h2>SSH key</h2><div class="actions">'
        f'{copy}{_form(ctx, "/ssh/remove", "Remove", css="danger small", confirm="Remove the SSH key? Anywhere that trusts it needs the new public key after this.")}'
        f"</div></div>{shown}{fingerprint}</section>"
    )


def _browsers(ctx: dict) -> str:
    """The browsers with a session, and signing out one or all of them."""
    rows = []
    for browser in ctx["browsers"]:
        this = ' <span class="tag">This browser</span>' if browser.get("current") else ""
        fields = {"id": browser["id"]}
        rows.append(
            f'<li class="row"><div class="rowmain"><div>{e(browser_name(browser["label"]))}{this}</div>'
            f'<div class="rowmeta">Signed in {e(date_text(browser["created"]))}</div></div>'
            f'<div class="actions">{_form(ctx, "/browsers/signout", "Sign out", css="secondary small", fields=fields)}</div></li>'
        )
    everyone = _form(ctx, "/browsers/signout-all", "Sign out all", css="danger small", confirm="Sign out every browser? The box stays signed in to Claude.")
    return (
        f'<section class="panel" id="browsers"><div class="panelhead"><h2>Browsers</h2><div class="actions">{everyone}</div></div>'
        f'<ul class="rows">{"".join(rows)}</ul></section>'
    )


def _claude(ctx: dict) -> str:
    """Whose Claude account the box uses, and signing it out."""
    box = ctx["box"]
    known = [e(box[name]) for name in ("email", "orgName", "subscriptionType") if box.get(name)]
    who = " · ".join(known) if box.get("loggedIn") and known else "Not signed in to Claude"
    if box.get("unknown"):
        who = "Couldn't check Claude's sign-in just now."
    confirm = "Sign the box out of Claude? Remote Control stops, and every browser is signed out. The keys are kept, and only this account can sign it back in."
    return (
        '<section class="panel" id="claude"><div class="panelhead"><div><h2>Claude</h2>'
        f'<p class="sub">{who}</p></div><div class="actions">{_form(ctx, "/box/signout", "Sign the box out", css="danger small", confirm=confirm)}</div></div></section>'
    )


def _signed_in(ctx: dict) -> str:
    """The whole page for a browser with a session."""
    box = ctx["box"]
    signin = _signin(ctx) if not box.get("loggedIn") and not box.get("unknown") else ""
    return (
        f'<main>{_notices(ctx)}{signin}{_console(ctx)}'
        f'<div class="twoup">{_files(ctx)}{_environment(ctx)}</div>'
        f'<div class="twoup">{_ssh(ctx)}{_browsers(ctx)}</div>{_claude(ctx)}</main>'
        '<dialog id="confirm" aria-labelledby="confirm-text"><p id="confirm-text"></p>'
        '<form method="dialog" class="formactions"><button class="secondary" value="cancel">Cancel</button>'
        '<button class="solid-danger" id="confirm-ok" value="ok">Confirm</button></form></dialog>'
    )


def page(ctx: dict) -> str:
    """The whole page for one request's context."""
    if ctx["session"]:
        rc = ctx["rc"]
        body_tag = f'<body data-state="{e(rc.get("state", ""))}" data-signed-in="{1 if ctx["box"].get("loggedIn") else 0}">'
        meta = f'<meta name="csrf-token" content="{e(ctx["csrf"])}">'
        content = _topbar(ctx) + _signed_in(ctx)
        scripts = (
            '<script src="/static/xterm.js" defer></script>'
            '<script src="/static/xterm-addon-fit.js" defer></script>'
            '<script src="/static/manager.js" defer></script>'
        )
    else:
        body_tag = '<body class="outside">'
        meta = ""
        content = (
            f'<main class="narrow"><div class="lockup">{MARK}<span>opsbox</span></div>'
            f"{_notices(ctx)}{_signin(ctx)}</main>"
        )
        scripts = '<script src="/static/manager.js" defer></script>'
    head = (
        '<!doctype html><html lang="en"><head><meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width, initial-scale=1">'
        f'{meta}<title>opsbox</title><link rel="icon" href="/static/icon.svg" type="image/svg+xml">'
        '<link rel="stylesheet" href="/static/xterm.css"><link rel="stylesheet" href="/static/manager.css"></head>'
    )
    return f"{head}{body_tag}{content}{scripts}</body></html>"
