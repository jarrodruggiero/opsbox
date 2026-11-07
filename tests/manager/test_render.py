"""Tests for the page: what it shows, to whom, and that everything in it is safe."""
from __future__ import annotations

import re
from pathlib import Path

import pytest

from opsbox.manager import render
from opsbox.manager.supervisor import STATE_LABELS

CHROME_WINDOWS = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/156.0.0.0 Safari/537.36"


def ctx(**overrides) -> dict:
    """A full signed-in context, with something in every section."""
    context: dict = {
        "session": True,
        "csrf": "session-csrf",
        "presession": "pre-csrf",
        "flash": None,
        "error": None,
        "box": {"loggedIn": True, "email": "me@example.com", "orgName": "Acme", "subscriptionType": "max"},
        "rc": {"state": "running", "restarts": 2, "last_exit": 1, "started_at": 1000.0, "pid": 42, "workdir": "/w", "question": None},
        "login": {"active": False, "mine": False, "url": None},
        "ssh": {"set": True, "public": "ssh-ed25519 AAAA opsbox", "fingerprint": "SHA256:abc"},
        "files": [{"path": "~/.kube/config", "size": 2048, "uploaded": 1762400000}],
        "env": [
            {"name": "TZ", "hidden": False, "value": "UTC"},
            {"name": "GH_TOKEN", "hidden": True, "value": None},
        ],
        "browsers": [
            {"id": "a" * 16, "label": CHROME_WINDOWS, "created": 1762400000, "expires": 1, "current": True},
            {"id": "b" * 16, "label": "curl/8", "created": 1762300000, "expires": 1, "current": False},
        ],
    }
    context.update(overrides)
    return context


def signed_out(**overrides) -> dict:
    """Exactly what the server sends without a session."""
    context: dict = {
        "session": False,
        "csrf": "",
        "presession": "pre-csrf",
        "flash": None,
        "error": None,
        "box": {"loggedIn": True, "owned": True},
        "rc": {},
        "login": {"active": False, "mine": False, "url": None},
        "ssh": {},
        "files": [],
        "env": [],
        "browsers": [],
    }
    context.update(overrides)
    return context


SIGNING_IN = {"active": True, "mine": True, "url": "https://claude.com/cai/oauth/authorize?x=1"}


def render_static() -> Path:
    """The directory the page's static files ship from."""
    return Path(render.__file__).parent / "static"


def forms(page: str) -> list[tuple[str, str]]:
    """Every form in the page: its opening tag's attributes, and its body."""
    return re.findall(r"<form\b([^>]*)>(.*?)</form>", page, re.DOTALL)


def test_a_stranger_sees_only_the_sign_in() -> None:
    """Anyone on the LAN can load the page: without a session it says nothing about the box."""
    page = render.page(signed_out())
    assert "Sign in to Claude" in page and 'action="/signin/start"' in page
    # And render itself hides them, even if handed a context that holds them.
    leaky = render.page(ctx(session=False, csrf=""))
    for shown in (page, leaky):
        for secret in ("me@example.com", "Acme", "Upload files", "Environment", "pane-rc", "data-state", "~/.kube/config", "ssh-ed25519", "csrf-token"):
            assert secret not in shown, secret


def test_a_box_with_no_owner_invites_setup_and_one_with_an_owner_doesnt() -> None:
    fresh = render.page(signed_out(box={"loggedIn": False, "owned": False}))
    assert "Sign in to set up this box." in fresh
    # After "Sign the box out" it isn't set up from scratch: it's still its owner's.
    owned = render.page(signed_out(box={"loggedIn": False, "owned": True}))
    assert "Sign in with the Claude account this box uses." in owned
    assert "set up this box" not in owned


def test_signing_in_shows_the_link_and_the_code_field() -> None:
    page = render.page(signed_out(login=SIGNING_IN))
    assert 'href="https://claude.com/cai/oauth/authorize?x=1"' in page
    assert 'target="_blank"' in page and 'rel="noopener noreferrer"' in page
    assert 'action="/signin/code"' in page and 'name="code"' in page and "autofocus" in page


def test_another_browsers_sign_in_shows_no_link() -> None:
    page = render.page(signed_out(login={"active": True, "mine": False, "url": None}))
    assert "Another browser is signing in." in page
    assert 'action="/signin/start"' in page
    assert 'action="/signin/code"' not in page and "claude.com" not in page


@pytest.mark.parametrize("url", ["javascript:alert(1)", "https://claude.com.evil.example/x", "http://claude.com/", ""])
def test_only_claude_links_are_rendered(url: str) -> None:
    """The link comes from the CLI's output, and only a claude.com or claude.ai address may become an href."""
    page = render.page(signed_out(login={"active": True, "mine": True, "url": url}))
    assert "href=\"" + url not in page or url == ""
    assert "unexpected sign-in link" in page
    assert "Open claude.ai" not in page


def test_a_session_on_a_signed_out_box_can_sign_it_back_in() -> None:
    """A session outlives Claude's sign-in if it expires: the page must offer the way back, with the right token."""
    page = render.page(ctx(box={"loggedIn": False}))
    start = next(body for attrs, body in forms(page) if 'action="/signin/start"' in attrs)
    assert 'value="pre-csrf"' in start


def test_an_unknown_status_offers_no_sign_in() -> None:
    """A status that couldn't be read isn't signed out: there's nothing to sign in over."""
    page = render.page(ctx(box={"loggedIn": False, "unknown": True}))
    assert 'action="/signin/start"' not in page
    assert "Couldn't check Claude's sign-in" in page
    assert "Not signed in to Claude" not in page


def test_every_form_posts_with_its_token() -> None:
    """A form without its token would be refused by the server."""
    for page in (
        render.page(ctx()),
        render.page(ctx(ssh={"set": False}, rc={**ctx()["rc"], "state": "asking", "question": "Sure? (y/n)"})),
        render.page(signed_out()),
        render.page(signed_out(login=SIGNING_IN)),
        render.page(ctx(box={"loggedIn": False})),
    ):
        found = forms(page)
        assert found
        for attrs, body in found:
            if 'method="dialog"' in attrs:
                continue
            assert 'method="post"' in attrs, attrs
            wanted = "pre-csrf" if "/signin/" in attrs else "session-csrf"
            assert f'name="csrf" value="{wanted}"' in body, attrs


def test_one_primary_per_form() -> None:
    for page in (render.page(ctx()), render.page(signed_out(login=SIGNING_IN))):
        for attrs, body in forms(page):
            assert len(re.findall(r'class="[^"]*\bprimary\b', body)) <= 1, attrs


def test_every_destructive_action_asks_first_and_says_what_survives() -> None:
    """Each danger button belongs to a form with a confirm, and the words say what's kept."""
    page = render.page(ctx())
    dangerous = [(attrs, body) for attrs, body in forms(page) if 'class="danger' in body]
    # One file, two settings, the SSH key, every browser and the box.
    assert len(dangerous) == 6
    for attrs, _body in dangerous:
        assert "data-confirm=" in attrs, attrs
    for words in ("The other files are kept.", "The other settings are kept.", "The box stays signed in to Claude.", "The keys are kept"):
        assert words in page


def test_there_is_one_real_dialog_for_the_confirm() -> None:
    page = render.page(ctx())
    assert page.count('<dialog id="confirm"') == 1
    assert 'id="confirm-text"' in page and 'id="confirm-ok"' in page
    assert render.page(signed_out()).count("<dialog") == 0


def test_every_visible_field_has_a_label() -> None:
    """Each field has a <label for> naming its id, so it's announced and a tap on the label focuses it."""
    for page in (render.page(ctx()), render.page(signed_out(login=SIGNING_IN))):
        labelled = set(re.findall(r'<label for="([^"]+)"', page))
        for tag, attrs in re.findall(r"<(input|textarea|select)\b([^>]*)>", page):
            if 'type="hidden"' in attrs or 'type="checkbox"' in attrs:
                continue
            found = re.search(r'\bid="([^"]+)"', attrs)
            assert found and found.group(1) in labelled, f"<{tag}{attrs}> has no label"


@pytest.mark.parametrize("state, shown, hidden", [
    ("running", {"stop", "restart"}, {"start"}),
    ("asking", {"stop", "restart"}, {"start"}),
    ("backing-off", {"stop", "restart"}, {"start"}),
    ("stopped", {"start"}, {"stop", "restart"}),
    ("signed-out", {"stop"}, {"start", "restart"}),
])
def test_only_the_buttons_that_make_sense_show(state: str, shown: set[str], hidden: set[str]) -> None:
    page = render.page(ctx(rc={**ctx()["rc"], "state": state}))
    for attrs, _body in forms(page):
        found = re.search(r'action="/rc/(start|stop|restart)"', attrs)
        if found:
            assert (" hidden" in attrs) == (found.group(1) in hidden), attrs
    assert {"start", "stop", "restart"} == shown | hidden


def test_the_buttons_list_the_states_manager_js_applies() -> None:
    """The page and the script must agree, or a button comes back wrongly after the first poll."""
    page = render.page(ctx())
    for name, states in render.ACTION_STATES.items():
        attrs = next(a for a, _b in forms(page) if f'action="/rc/{name}"' in a)
        assert f'data-show="{" ".join(states)}"' in attrs
        assert set(states) <= set(STATE_LABELS)


def test_a_question_shows_its_words_and_yes_and_no() -> None:
    quiet = render.page(ctx())
    assert 'id="rc-question" hidden' in quiet
    asked = render.page(ctx(rc={**ctx()["rc"], "state": "asking", "question": "Trust <the> dir? [y/N]"}))
    assert 'id="rc-question" hidden' not in asked
    assert "Trust &lt;the&gt; dir? [y/N]" in asked
    answers = [body for attrs, body in forms(asked) if 'action="/rc/answer"' in attrs]
    assert len(answers) == 2
    assert 'name="answer" value="yes"' in answers[0] and 'name="answer" value="no"' in answers[1]
    # Tied to what's on the screen, so a double-click can't answer the next one.
    for answer in answers:
        assert 'name="question" value="Trust &lt;the&gt; dir? [y/N]"' in answer


@pytest.mark.parametrize("state", sorted(STATE_LABELS))
def test_a_state_has_a_word_and_a_symbol_not_just_colour(state: str) -> None:
    symbol, word = STATE_LABELS[state]
    page = render.page(ctx(rc={**ctx()["rc"], "state": state}))
    assert word in page and symbol in page


def test_the_console_has_both_terminals_with_real_tabs() -> None:
    page = render.page(ctx())
    assert 'id="pane-rc"' in page and 'id="pane-shell"' in page
    assert 'id="pane-shell" role="tabpanel" aria-labelledby="tab-shell" hidden' in page
    assert 'role="tab"' in page and 'aria-controls="pane-shell"' in page


def test_the_settings_form_takes_any_name_and_value() -> None:
    """Both are typed: there is no list to choose from."""
    page = render.page(ctx())
    assert "<select" not in page
    form = next(body for attrs, body in forms(page) if 'action="/env"' in attrs)
    assert '<input id="env-name" name="name"' in form and '<input id="env-value" name="value"' in form
    assert 'type="checkbox" name="hidden" value="1"' in form


def test_a_hidden_setting_shows_only_that_it_is_hidden() -> None:
    page = render.page(ctx())
    assert "Australia" not in page and "GH_TOKEN" in page and "Hidden" in page


def test_the_upload_form_uses_a_real_file_picker_and_a_path() -> None:
    page = render.page(ctx())
    attrs, body = next(form for form in forms(page) if 'action="/files"' in form[0])
    assert 'enctype="multipart/form-data"' in attrs
    assert 'type="file" name="file"' in body and 'name="path"' in body


def test_a_file_shows_its_path_size_and_date_with_replace_and_remove() -> None:
    page = render.page(ctx())
    assert "~/.kube/config" in page and "2.0 KB" in page and "Nov 2025" in page
    assert 'data-replace="~/.kube/config"' in page
    assert 'action="/files/remove"' in page


def test_empty_sections_say_so() -> None:
    page = render.page(ctx(files=[], env=[], ssh={"set": False}))
    for words in ("No files yet.", "No settings yet.", "No key yet."):
        assert words in page
    assert 'action="/ssh/generate"' in page
    assert "Copy public key" not in page


def test_the_ssh_key_shows_its_public_half_and_copy() -> None:
    page = render.page(ctx())
    assert 'data-copy="ssh-ed25519 AAAA opsbox"' in page and "SHA256:abc" in page
    assert 'action="/ssh/generate"' not in page


def test_browsers_read_as_a_person_would_say_them() -> None:
    page = render.page(ctx())
    assert "Chrome on Windows" in page and CHROME_WINDOWS not in page
    assert page.count("This browser") == 1


@pytest.mark.parametrize("agent, name", [
    (CHROME_WINDOWS, "Chrome on Windows"),
    ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/18.0 Safari/605.1.15", "Safari on macOS"),
    ("Mozilla/5.0 (iPhone; CPU iPhone OS 18_0 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/18.0 Mobile/15E148 Safari/604.1", "Safari on iPhone"),
    ("Mozilla/5.0 (Linux; Android 16; Pixel 9) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/156.0.0.0 Mobile Safari/537.36", "Chrome on Android"),
    ("Mozilla/5.0 (X11; Linux x86_64; rv:140.0) Gecko/20100101 Firefox/140.0", "Firefox on Linux"),
    ("Mozilla/5.0 (Windows NT 10.0) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/156.0.0.0 Safari/537.36 Edg/156.0.0.0", "Edge on Windows"),
    ("curl/8.0", "A browser"),
    ("", "A browser"),
])
def test_browser_names(agent: str, name: str) -> None:
    assert render.browser_name(agent) == name


@pytest.mark.parametrize("size, text", [(0, "0 B"), (1023, "1023 B"), (1024, "1.0 KB"), (5512, "5.4 KB"), (1048576, "1.0 MB")])
def test_file_sizes(size: int, text: str) -> None:
    assert render.size_text(size) == text


def test_every_value_is_escaped() -> None:
    """Text and attributes alike: a path, a name, a value or a label can't become markup."""
    bad = '"><script>x</script>'
    page = render.page(ctx(
        box={"loggedIn": True, "email": bad, "orgName": bad, "subscriptionType": bad},
        flash=bad, error=bad,
        rc={**ctx()["rc"], "state": "asking", "question": bad},
        ssh={"set": True, "public": bad, "fingerprint": bad},
        files=[{"path": bad, "size": 1, "uploaded": 1}],
        env=[{"name": bad, "hidden": False, "value": bad}],
        browsers=[{"id": bad, "label": bad, "created": 1, "expires": 1, "current": False}],
    ))
    assert "<script>x" not in page
    assert '" onmouseover' not in page
    assert page.count("&lt;script&gt;") >= 10
    # Stray quotes can't close an attribute either: the only tags are ours.
    assert not re.search(r'<(?!/?(?:html|head|meta|title|link|body|header|a|svg|path|span|main|div|section|h1|h2|p|form|input|button|ul|li|code|label|dialog|ol|noscript|script)\b)', page.split("<!doctype html>")[1])


def test_no_inline_script_or_style() -> None:
    """The CSP allows neither, so the page must have neither, signed in or not."""
    for page in (render.page(ctx()), render.page(signed_out(login=SIGNING_IN))):
        assert not re.search(r"<script(?![^>]*\bsrc=)", page)
        for banned in (" style=", " onclick=", " onload=", "javascript:", "<style"):
            assert banned not in page


def test_the_favicon_and_the_page_draw_the_same_mark() -> None:
    """Two copies of a drawing drift: every path on the page is in the icon file, and the other way round."""
    icon = (render_static() / "icon.svg").read_text()
    page_paths = re.findall(r"<path[^>]*/>", render.MARK_PATHS)
    assert re.findall(r"<path[^>]*/>", icon) == page_paths
    assert 'class="top"' in render.MARK_PATHS


def test_the_upload_form_works_without_its_script() -> None:
    """The plain control and its path are all a page with no script needs, and the rest stays out of the way."""
    page = render.page(ctx())
    form = page.split('id="upload-form"', 1)[1].split("</form>", 1)[0]
    assert '<input id="file-input" type="file" name="file" required>' in form
    assert 'autocomplete="off" required>' in form
    assert 'id="file-pickers" hidden' in form
    assert 'id="upload-result" role="status" hidden' in form
    assert "multiple" not in form
