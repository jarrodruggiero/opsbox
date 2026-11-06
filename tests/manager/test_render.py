"""Tests for render.page."""
from __future__ import annotations

import re

from opsbox.manager import render


def ctx(**overrides) -> dict:
    """Return a full signed-in context with sensible defaults."""
    c: dict = {
        "session": True,
        "csrf": "x",
        "flash": None,
        "error": None,
        "box": {"loggedIn": True, "email": "me@example.com", "orgName": "Acme", "subscriptionType": "Pro"},
        "rc": {"state": "running", "restarts": 2, "last_exit": 1, "started_at": 1000.0, "pid": 42},
        "login": {"active": False, "mine": False, "url": None},
        "keys": [
            {
                "id": "kubernetes",
                "label": "Kubernetes",
                "hint": "",
                "set": True,
                "context": "acme",
                "server": "https://k8s.example.com",
                "user": "admin",
            },
            {
                "id": "ssh",
                "label": "SSH",
                "hint": "",
                "set": False,
            },
        ],
        "env": {"TZ": "UTC"},
        "env_allowed": {"TZ": "The timezone for logs and local times.", "DOCKER_HOST": "Where the docker CLI builds and runs containers."},
        "browsers": [{"label": "Chrome", "created": 1700000000}],
    }
    c.update(overrides)
    return c


def signed_out(**overrides) -> dict:
    """Return what the server sends without a session."""
    c: dict = {
        "session": False,
        "csrf": "x",
        "flash": None,
        "error": None,
        "box": {"loggedIn": True},
        "rc": {},
        "login": {"active": False, "mine": False, "url": None},
        "keys": [],
        "env": {},
        "env_allowed": {},
        "browsers": [],
    }
    c.update(overrides)
    return c


class TestSignedOut:
    def test_signed_out_shows_only_signin(self) -> None:
        """Anyone on the LAN can load the page: without a session it shows the sign-in and nothing about the box."""
        page = render.page(signed_out())
        assert "Sign in to Claude" in page
        assert 'action="/signin/start"' in page
        assert "Keys" not in page
        assert 'id="terminal"' not in page
        assert "Running" not in page
        assert "data-state" not in page

        # Also test with a full signed-in context but session=False
        page2 = render.page(ctx(session=False))
        assert "me@example.com" not in page2
        assert "Keys" not in page2
        assert 'id="terminal"' not in page2


class TestOtherBrowserSignin:
    def test_other_browsers_signin_shows_no_link(self) -> None:
        """Another browser's sign-in shows no link and no code form, so it can't be finished from here."""
        page = render.page(signed_out(login={"active": True, "mine": False, "url": None}))
        assert "Another browser is signing in." in page
        assert 'action="/signin/start"' in page
        assert 'action="/signin/code"' not in page
        assert "claude.com" not in page


class TestLoginActive:
    def test_login_active_shows_link_and_code_form(self) -> None:
        """The browser that started a sign-in gets the claude.ai link, in a new tab, and the code form."""
        page = render.page(signed_out(login={"active": True, "mine": True, "url": "https://claude.com/cai/oauth/authorize?x=1"}))
        assert 'href="https://claude.com/cai/oauth/authorize?x=1"' in page
        assert 'target="_blank"' in page
        assert "noopener" in page
        assert 'action="/signin/code"' in page
        assert 'name="code"' in page


class TestCsrf:
    def test_every_form_posts_with_csrf(self) -> None:
        """A form without its csrf field would be refused by the server, so every one must carry it."""
        c = ctx()
        page = render.page(c)
        form_pattern = re.compile(r'<form[^>]*>(.*?)</form>', re.DOTALL)
        for m in form_pattern.finditer(page):
            form_body = m.group(0)
            assert 'method="post"' in form_body
            assert 'name="csrf"' in form_body
            assert c["csrf"] in form_body


class TestOnePrimary:
    def test_one_primary_per_form(self) -> None:
        """One committing action per form: two primaries means nobody can tell which button does it."""
        c = ctx()
        page = render.page(c)
        form_pattern = re.compile(r'<form[^>]*>(.*?)</form>', re.DOTALL)
        for m in form_pattern.finditer(page):
            form_body = m.group(1)
            primary_count = len(re.findall(r'class="primary"', form_body)) + len(re.findall(r'class="button primary"', form_body))
            assert primary_count <= 1, "Form has more than one primary button"


class TestOnlyClaudeLinks:
    def test_only_claude_links_are_rendered(self) -> None:
        """Only a claude.com or claude.ai address ever becomes the link, whatever the CLI printed."""
        page1 = render.page(signed_out(login={"active": True, "mine": True, "url": "javascript:alert(1)"}))
        assert "javascript:" not in page1
        assert "unexpected sign-in link" in page1

        page2 = render.page(signed_out(login={"active": True, "mine": True, "url": "https://claude.com.evil.example/x"}))
        assert "evil" not in page2
        assert 'href="https://claude.com.evil.example/x"' not in page2


class TestTrustButton:
    def test_trust_button_only_when_asked(self) -> None:
        """Trust is offered only when Remote Control asks; never as a standing button."""
        page_asked = render.page(ctx(rc={"state": "trust-asked", "restarts": 0, "last_exit": None, "started_at": None, "pid": None}))
        assert 'action="/rc/trust"' in page_asked

        page_running = render.page(ctx(rc={"state": "running", "restarts": 0, "last_exit": None, "started_at": None, "pid": None}))
        assert 'action="/rc/trust"' not in page_running


class TestStateWord:
    def test_state_has_a_word_not_just_colour(self) -> None:
        """Every state has a word, so colour never carries the meaning alone."""
        for state, word in [
            ("running", "Running"),
            ("stopped", "Stopped"),
            ("backing-off", "Restarting soon"),
            ("trust-asked", "Waiting for you"),
            ("signed-out", "Not signed in"),
        ]:
            page = render.page(ctx(rc={"state": state, "restarts": 0, "last_exit": None, "started_at": None, "pid": None}))
            assert word in page, f"State {state} should show '{word}'"


class TestEscaping:
    def test_values_are_escaped(self) -> None:
        """Context values are escaped in text and in attributes, so a value can't become markup."""
        c = ctx()
        c["box"]["email"] = "<script>x</script>"
        c["env"]["XSS"] = '" onmouseover="x'
        c["keys"] = [
            {
                "id": "kubernetes",
                "label": "Kubernetes",
                "hint": "",
                "set": True,
                "context": "<b>",
                "server": "https://k8s.example.com",
                "user": "admin",
            },
        ]
        page = render.page(c)
        assert "&lt;script&gt;" in page
        assert "&quot;" in page
        assert "<script>x" not in page
        assert '" onmouseover' not in page


class TestSshCopy:
    def test_ssh_shows_copy_when_set(self) -> None:
        """With an SSH key, the page offers its public half to copy, and its fingerprint, but no second Generate."""
        c = ctx()
        c["keys"] = [
            {
                "id": "kubernetes",
                "label": "Kubernetes",
                "hint": "",
                "set": True,
                "context": "acme",
                "server": "https://k8s.example.com",
                "user": "admin",
            },
            {
                "id": "ssh",
                "label": "SSH",
                "hint": "",
                "set": True,
                "public": "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAI test",
                "fingerprint": "SHA256:abc123",
            },
        ]
        page = render.page(c)
        assert 'data-copy="ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAI test"' in page
        assert "SHA256:abc123" in page
        assert "Generate" not in page


class TestConfirmText:
    def test_confirms_say_what_survives(self) -> None:
        """Each destructive confirm says what survives it."""
        page = render.page(ctx())
        assert "The other keys are kept." in page
        assert "The box stays signed in to Claude." in page
        assert "The keys are kept, and only this account can sign it back in." in page


class TestNoInline:
    def test_no_inline_script_or_style(self) -> None:
        """The CSP allows no inline script or style, so the page must have none."""
        page = render.page(ctx())
        script_tags = re.findall(r'<script[^>]*>', page)
        for tag in script_tags:
            assert 'src="' in tag, f"<script> tag without src: {tag}"
        assert ' style=' not in page
        assert ' onclick=' not in page
        assert "javascript:" not in page


def test_every_field_has_a_label() -> None:
    """Each visible field has a <label for> naming its id, so it's announced and a tap on the label focuses it."""
    for page in (render.page(ctx()), render.page(signed_out(login={"active": True, "mine": True, "url": "https://claude.com/x"}))):
        labelled = set(re.findall(r'<label for="([^"]+)"', page))
        fields = re.findall(r"<(input|textarea|select)\b([^>]*)>", page)
        for tag, attrs in fields:
            if 'type="hidden"' in attrs:
                continue
            found = re.search(r'\bid="([^"]+)"', attrs)
            assert found and found.group(1) in labelled, f"<{tag}{attrs}> has no label"


def test_missing_fields_do_not_break_the_page() -> None:
    """describe() leaves out what it can't read, and a box signed in without claude.ai has no email."""
    page = render.page(ctx(box={"loggedIn": True}, keys=[{"id": "kubernetes", "label": "Kubernetes", "hint": "", "set": True, "context": "home", "user": "opsbox"}]))
    assert "Not signed in to Claude" in page
    assert "home · opsbox" in page
    assert "None" not in page


def test_no_environment_means_no_table() -> None:
    """With nothing set there is no empty table, only the form to set one."""
    page = render.page(ctx(env={}))
    assert "<table" not in page
    assert 'action="/env"' in page


def test_trust_names_the_real_directory() -> None:
    """The trust question names the directory Remote Control runs in, which OPSBOX_WORKDIR can change."""
    page = render.page(ctx(rc={"state": "trust-asked", "restarts": 0, "last_exit": None, "workdir": "/home/opsbox/<w>"}))
    assert "asks to trust /home/opsbox/&lt;w&gt;." in page


def test_copy_value_is_escaped_in_its_attribute() -> None:
    """A quote in the public key can't end the data-copy attribute and start another."""
    ssh = {"id": "ssh", "label": "SSH", "hint": "", "set": True, "public": 'ssh-ed25519 AAAA" onclick="x', "fingerprint": "SHA256:abc"}
    page = render.page(ctx(keys=[ssh]))
    assert 'data-copy="ssh-ed25519 AAAA&quot; onclick=&quot;x"' in page
    assert '" onclick="x' not in page
