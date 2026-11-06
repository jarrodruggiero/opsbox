"""Tests for the box's SSH key and its free-form settings."""
from __future__ import annotations

import pathlib

import pytest

from opsbox.manager import keys
from opsbox.manager.keys import Refused


@pytest.fixture(autouse=True)
def home(tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch) -> pathlib.Path:
    """HOME is a scratch directory, so nothing here touches a real one."""
    directory = tmp_path / "home"
    directory.mkdir()
    monkeypatch.setenv("HOME", str(directory))
    return directory


def env_file(home: pathlib.Path) -> str:
    return (home / ".config/opsbox/env").read_text()


def test_ssh_key_is_generated_and_only_its_public_half_shows(home: pathlib.Path) -> None:
    """The private key stays on the box at 0600; what describe returns can't contain it."""
    assert keys.describe_ssh() == {"set": False}
    keys.generate_ssh()
    private = home / ".ssh/id_ed25519"
    assert private.stat().st_mode & 0o777 == 0o600
    info = keys.describe_ssh()
    assert info["set"] is True
    assert info["public"].startswith("ssh-ed25519 ")
    assert info["fingerprint"].startswith("SHA256:")
    assert "PRIVATE KEY" not in str(info)
    assert private.read_text().strip().split("\n")[1] not in str(info)


def test_a_second_key_is_refused_rather_than_replacing_the_first(home: pathlib.Path) -> None:
    """Replacing the key would silently break everywhere that trusts it."""
    keys.generate_ssh()
    first = (home / ".ssh/id_ed25519").read_text()
    with pytest.raises(Refused, match="already an SSH key"):
        keys.generate_ssh()
    assert (home / ".ssh/id_ed25519").read_text() == first


def test_removing_the_key_removes_both_halves(home: pathlib.Path) -> None:
    keys.generate_ssh()
    keys.remove_ssh()
    assert keys.describe_ssh() == {"set": False}
    assert not (home / ".ssh/id_ed25519.pub").exists()


def test_a_missing_keygen_is_a_refusal(monkeypatch: pytest.MonkeyPatch) -> None:
    """A broken image says so on the page, rather than a 500."""
    monkeypatch.setattr(keys, "SSH_KEYGEN", "/nonexistent/ssh-keygen")
    with pytest.raises(Refused, match="ssh-keygen"):
        keys.generate_ssh()


def test_any_name_and_value_can_be_set(home: pathlib.Path) -> None:
    """Settings aren't a fixed list: both the name and the value are the person's."""
    keys.set_env("MY_SETTING_2", "a value with spaces = and an equals")
    assert keys.env_rows() == [{"name": "MY_SETTING_2", "hidden": False, "value": "a value with spaces = and an equals"}]
    assert env_file(home) == "MY_SETTING_2=a value with spaces = and an equals\n"
    assert (home / ".config/opsbox/env").stat().st_mode & 0o777 == 0o600


def test_saving_again_replaces_and_keeps_the_others(home: pathlib.Path) -> None:
    keys.set_env("A", "1")
    keys.set_env("B", "2")
    keys.set_env("A", "3")
    assert env_file(home) == "A=3\nB=2\n"


@pytest.mark.parametrize("name", ["", "1ABC", "A-B", "A B", "A=B", "A\nB", "A\x00", "é", "X" * 129])
def test_a_name_a_shell_couldnt_use_is_refused(name: str) -> None:
    """A name is what ends up on the left of an = in the file: anything else could forge a line."""
    with pytest.raises(Refused, match="letters, digits and underscores"):
        keys.set_env(name, "value")


@pytest.mark.parametrize("name", sorted(keys.ENV_REFUSED))
def test_what_breaks_remote_control_is_refused_with_why(name: str) -> None:
    """Free-form stops short of what would silently turn it off or replace its sign-in."""
    with pytest.raises(Refused) as refused:
        keys.set_env(name, "x")
    assert keys.ENV_REFUSED[name] in str(refused.value)


@pytest.mark.parametrize("value, message", [
    ("", "Give it a value"),
    ("   ", "Give it a value"),
    ("a\nb", "line break"),
    ("a\rb", "line break"),
    ("a\x00b", "control character"),
    ("a\x0cb", "control character"),
    ("a\x1cb", "control character"),
    ("a\x85b", "control character"),
    ("a\u2028b", "control character"),
    ("x" * 4097, "too long"),
])
def test_a_value_the_file_couldnt_hold_is_refused(value: str, message: str) -> None:
    """Anything one reader takes for a line break would forge another setting, and a NUL would stop Remote Control starting."""
    with pytest.raises(Refused, match=message):
        keys.set_env("NAME", value)


def test_a_hidden_value_never_comes_back(home: pathlib.Path) -> None:
    """Ticking Hide keeps the value out of everything the page is given, but not out of the file."""
    keys.set_env("PLAIN_NAME", "SENTINEL-7f3a", hidden=True)
    assert "SENTINEL-7f3a" not in str(keys.env_rows())
    assert keys.env_rows()[0]["hidden"] is True
    assert "SENTINEL-7f3a" in env_file(home)


@pytest.mark.parametrize("name, value", [
    ("GH_TOKEN", "x"),
    ("my_secret", "x"),
    ("DB_PASSWORD", "x"),
    ("ANTHROPIC_KEY", "x"),
    ("OPENAI_API_KEY", "x"),
    ("DB_PASS", "x"),
    ("MYSQL_PWD", "x"),
    ("SOME_JWT", "x"),
    ("HTTPS_PROXY", "http://user:SENTINEL@proxy:3128"),
    ("CACHE_URL", "redis://:SENTINEL@cache:6379"),
    ("REPO_URL", "https://ghp_SENTINEL@github.com/me/repo.git"),
])
def test_what_looks_secret_is_hidden_whether_or_not_it_was_ticked(name: str, value: str) -> None:
    """The box might be forgotten: a token's name, or a password in a URL, hides it anyway."""
    keys.set_env(name, value)
    assert keys.env_rows() == [{"name": name, "hidden": True, "value": None}]


@pytest.mark.parametrize("name, value", [("GIT_AUTHOR_NAME", "Jarrod"), ("TZ", "Australia/Melbourne"), ("DOCKER_HOST", "ssh://x@y"), ("HTTPS_PROXY", "http://proxy:3128")])
def test_ordinary_settings_stay_visible(name: str, value: str) -> None:
    """Over-hiding has a cost too: AUTHOR must not match the "auth" pattern, nor ssh://x@y the password one."""
    keys.set_env(name, value)
    assert keys.env_rows() == [{"name": name, "hidden": False, "value": value}]


def test_a_hidden_setting_stays_hidden_when_saved_again(home: pathlib.Path) -> None:
    """A rotated token saved without ticking the box mustn't appear on the page."""
    keys.set_env("PLAIN", "one", hidden=True)
    keys.set_env("PLAIN", "SENTINEL-7f3a")
    assert keys.env_rows() == [{"name": "PLAIN", "hidden": True, "value": None}]


def write_env(home: pathlib.Path, text: str) -> None:
    (home / ".config/opsbox").mkdir(parents=True, exist_ok=True)
    (home / ".config/opsbox/env").write_text(text)


def test_a_file_written_by_hand_or_an_older_version_is_still_hidden(home: pathlib.Path) -> None:
    """With no hidden list at all, what looks secret must not reach the page."""
    write_env(home, "GH_TOKEN=SENTINEL-7f3a\nHTTPS_PROXY=http://u:SENTINEL-7f3a@proxy\nTZ=UTC\n")
    assert "SENTINEL-7f3a" not in str(keys.env_rows())
    assert {"name": "TZ", "hidden": False, "value": "UTC"} in keys.env_rows()


def test_an_unreadable_hidden_list_hides_everything(home: pathlib.Path) -> None:
    """A corrupt list fails closed, and saving something else doesn't reveal the rest."""
    keys.set_env("QUIET", "SENTINEL-7f3a", hidden=True)
    (home / ".config/opsbox/env-hidden.json").write_text("{not json")
    assert keys.env_rows() == [{"name": "QUIET", "hidden": True, "value": None}]
    keys.set_env("TZ", "UTC")
    rows = {row["name"]: row for row in keys.env_rows()}
    assert rows["QUIET"]["hidden"] is True
    assert rows["TZ"] == {"name": "TZ", "hidden": False, "value": "UTC"}


def test_what_starts_a_process_is_what_the_page_would_allow(home: pathlib.Path) -> None:
    """The file can be edited by hand: a refused name or a broken line never reaches Remote Control or a shell."""
    write_env(home, "GOOD=a=b==\nANTHROPIC_API_KEY=x\nHOME=/elsewhere\nbad-name=x\n# a comment\nno equals here\nFORM=a\x0cb\n")
    assert keys.read_env_file(keys.env_path()) == {"GOOD": "a=b==", "FORM": "a\x0cb"}
    assert keys.read_env_file(home / "missing") == {}


def test_a_file_that_isnt_text_reads_as_what_it_can(home: pathlib.Path) -> None:
    """A stray byte in the settings mustn't stop Remote Control starting, or the page rendering."""
    (home / ".config/opsbox").mkdir(parents=True)
    (home / ".config/opsbox/env").write_bytes(b"GOOD=yes\nBAD=\xff\xfe\n")
    assert keys.read_env_file(keys.env_path())["GOOD"] == "yes"
    assert {"name": "GOOD", "hidden": False, "value": "yes"} in keys.env_rows()


def test_a_public_key_that_isnt_text_doesnt_break_the_page(home: pathlib.Path) -> None:
    """Anything can be uploaded to ~/.ssh, and describing it runs on every page load."""
    (home / ".ssh").mkdir()
    (home / ".ssh/id_ed25519").write_text("private")
    (home / ".ssh/id_ed25519.pub").write_bytes(b"\xff\xfe\x00not text")
    assert keys.describe_ssh()["set"] is True


def test_removing_forgets_it_entirely(home: pathlib.Path) -> None:
    """Removed, and saved again with the same name, it isn't still hidden."""
    keys.set_env("PLAIN", "one", hidden=True)
    keys.set_env("OTHER", "two")
    keys.remove_env("PLAIN")
    assert env_file(home) == "OTHER=two\n"
    assert "PLAIN" not in (home / ".config/opsbox/env-hidden.json").read_text()
    keys.set_env("PLAIN", "three")
    assert {"name": "PLAIN", "hidden": False, "value": "three"} in keys.env_rows()
