"""Tests for opsbox.manager.keys."""
from __future__ import annotations

import pathlib
import textwrap

import pytest

from opsbox.manager import keys
from opsbox.manager.keys import (
    KeyError_,
    describe,
    describe_all,
    generate_ssh,
    get_env,
    remove_key,
    set_env,
    set_key,
)


@pytest.fixture
def fake_gh(tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch) -> pathlib.Path:
    """Write a fake `gh` script and point keys.GH at it."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()

    script = bin_dir / "gh"
    script.write_text(
        textwrap.dedent(
            f"""\
            #!/usr/bin/env python3
            import sys
            import os

            tmp_path = r"{tmp_path}"
            gh_ok = os.path.join(tmp_path, "gh-ok")

            args = sys.argv[1:]

            if args == ["auth", "login", "--hostname", "github.com", "--with-token"]:
                token = sys.stdin.read().strip()
                if token == "ghp_good" or "SENTINEL-7f3a" in token:
                    open(gh_ok, "w").close()
                    sys.exit(0)
                else:
                    sys.stderr.write("bad token\\n")
                    sys.exit(1)
            elif args == ["auth", "status", "--hostname", "github.com"]:
                if os.path.exists(gh_ok):
                    # What gh 2.x really prints.
                    print("github.com")
                    print("  \u2713 Logged in to github.com account someone (keyring)")
                    print("  - Active account: true")
                    sys.exit(0)
                else:
                    sys.exit(1)
            elif args == ["auth", "logout", "--hostname", "github.com"]:
                if os.path.exists(gh_ok):
                    os.unlink(gh_ok)
                sys.exit(0)
            else:
                sys.exit(1)
            """
        )
    )
    script.chmod(0o755)

    monkeypatch.setattr(keys, "GH", str(script))
    return bin_dir


@pytest.fixture
def setup_home(tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch) -> pathlib.Path:
    """Set HOME to tmp_path/home and patch home() accordingly."""
    home_dir = tmp_path / "home"
    home_dir.mkdir()
    monkeypatch.setenv("HOME", str(home_dir))
    # Also patch the module-level home() function
    import opsbox.manager.keys as keys_module

    monkeypatch.setattr(keys_module, "home", lambda: home_dir)
    return home_dir


def test_kubeconfig_is_saved_private_and_described(setup_home, fake_gh) -> None:
    """A minimal valid kubeconfig is saved at mode 0600 under a 0700 directory and described."""
    kubeconfig = textwrap.dedent(
        """\
        apiVersion: v1
        kind: Config
        current-context: home
        contexts:
        - name: home
          context:
            cluster: home-cluster
            user: opsbox-user
        clusters:
        - name: home-cluster
          cluster:
            server: https://10.0.0.1:6443
        - name: other-cluster
          cluster:
            server: https://10.9.9.9:6443
        users:
        - name: opsbox-user
          user:
            token: abc
        """
    )
    set_key("kubernetes", kubeconfig)

    key_path = setup_home / ".kube" / "config"
    assert key_path.exists()
    assert key_path.stat().st_mode & 0o777 == 0o600
    assert (setup_home / ".kube").stat().st_mode & 0o777 == 0o700

    info = describe("kubernetes")
    assert info["set"] is True
    assert info["context"] == "home"
    assert info["server"] == "https://10.0.0.1:6443"
    assert info["user"] == "opsbox-user"


def test_bad_kubeconfig_is_refused(setup_home, fake_gh) -> None:
    """'hello: world' raises KeyError_ and nothing is written."""
    with pytest.raises(KeyError_):
        set_key("kubernetes", "hello: world")
    key_path = setup_home / ".kube" / "config"
    assert not key_path.exists()


def test_values_never_come_back(setup_home, fake_gh) -> None:
    """describe_all() never contains the sentinel string used in every key value."""
    sentinel = "SENTINEL-7f3a"

    kubeconfig = textwrap.dedent(
        f"""\
        apiVersion: v1
        kind: Config
        current-context: home
        contexts:
        - name: home
          context:
            cluster: home-cluster
            user: opsbox-user
        clusters:
        - name: home-cluster
          cluster:
            server: https://10.0.0.1:6443
        users:
        - name: opsbox-user
          user:
            token: {sentinel}
        """
    )
    set_key("kubernetes", kubeconfig)

    talosconfig = textwrap.dedent(
        f"""\
        context: home
        contexts:
          home:
            endpoints:
            - 10.0.0.1
            ca: {sentinel}
            crt: {sentinel}
            key: {sentinel}
        """
    )
    set_key("talos", talosconfig)

    set_key("terraform", sentinel + "x" * 15)

    set_key("local-llm", sentinel + "x" * 15)

    # For github, the fake accepts the sentinel, so we can use it to verify
    # that describe for github never returns the token value.
    set_key("github", "ghp_SENTINEL-7f3a")

    text = str(describe_all())
    assert sentinel not in text


def test_github_token_goes_to_gh(setup_home, fake_gh) -> None:
    """'ghp_good' sets the token and describe shows account 'someone'; 'ghp_bad' raises."""
    assert describe("github")["set"] is False
    set_key("github", "ghp_good")
    info = describe("github")
    assert info["set"] is True
    assert info["account"] == "someone"

    with pytest.raises(KeyError_, match="bad token"):
        set_key("github", "ghp_bad")


def test_ssh_is_generated_and_only_public_is_shown(setup_home, fake_gh) -> None:
    """generate_ssh() creates a private key at mode 0600; describe shows only the public half."""
    generate_ssh()

    key_path = setup_home / ".ssh" / "id_ed25519"
    assert key_path.exists()
    assert key_path.stat().st_mode & 0o777 == 0o600

    info = describe("ssh")
    assert info["set"] is True
    assert info["public"].startswith("ssh-ed25519")
    assert "fingerprint" in info
    assert "PRIVATE KEY" not in str(info)

    with pytest.raises(KeyError_, match="already an SSH key"):
        generate_ssh()


def test_ssh_cannot_be_pasted(setup_home, fake_gh) -> None:
    """set_key('ssh', 'anything') raises KeyError_."""
    with pytest.raises(KeyError_):
        set_key("ssh", "anything")


def test_remove_key(setup_home, fake_gh) -> None:
    """After remove_key('kubernetes'), describe says set False and the file is gone."""
    kubeconfig = textwrap.dedent(
        """\
        apiVersion: v1
        kind: Config
        current-context: home
        contexts:
        - name: home
          context:
            cluster: home-cluster
            user: opsbox-user
        clusters:
        - name: home-cluster
          cluster:
            server: https://10.0.0.1:6443
        users:
        - name: opsbox-user
          user:
            token: abc
        """
    )
    set_key("kubernetes", kubeconfig)
    assert (setup_home / ".kube" / "config").exists()

    remove_key("kubernetes")
    assert not (setup_home / ".kube" / "config").exists()

    info = describe("kubernetes")
    assert info["set"] is False


def test_local_llm_sets_its_env(setup_home, fake_gh) -> None:
    """After setting the local-llm key, LOCAL_LLM_KEY_FILE is in env; remove_key removes it."""
    key_value = "a" * 20
    set_key("local-llm", key_value)

    env = get_env()
    assert "LOCAL_LLM_KEY_FILE" in env
    assert env["LOCAL_LLM_KEY_FILE"] == str(setup_home / ".config" / "local-llm" / "key")

    remove_key("local-llm")
    env = get_env()
    assert "LOCAL_LLM_KEY_FILE" not in env


def test_env_allowlist(setup_home, fake_gh) -> None:
    """set_env('TZ', 'Australia/Melbourne') works; disallowed names and bad values raise."""
    set_env("TZ", "Australia/Melbourne")
    assert get_env()["TZ"] == "Australia/Melbourne"

    with pytest.raises(KeyError_):
        set_env("PATH", "/x")

    with pytest.raises(KeyError_, match="claude.ai sign-in"):
        set_env("ANTHROPIC_API_KEY", "x")

    with pytest.raises(KeyError_):
        set_env("TZ", "a\nb")

    set_env("TZ", "")
    assert "TZ" not in get_env()


def test_env_keeps_managed_names(setup_home, fake_gh) -> None:
    """After setting the LLM key and then TZ, both names are still in the file."""
    set_key("local-llm", "a" * 20)
    set_env("TZ", "Australia/Melbourne")

    env = get_env()
    assert "LOCAL_LLM_KEY_FILE" in env
    assert "TZ" in env


def test_refuses_nul_and_huge(setup_home, fake_gh) -> None:
    """set_key with NUL and with 70000 bytes each raise KeyError_."""
    with pytest.raises(KeyError_, match="NUL"):
        set_key("kubernetes", "a\x00b")

    with pytest.raises(KeyError_, match="too long"):
        set_key("kubernetes", "x" * 70000)


def test_remove_key_ssh_deletes_pub(setup_home, fake_gh) -> None:
    """After remove_key('ssh'), both the key and .pub are gone."""
    generate_ssh()
    pub_path = setup_home / ".ssh" / "id_ed25519.pub"
    assert pub_path.exists()

    remove_key("ssh")
    assert not (setup_home / ".ssh" / "id_ed25519").exists()
    assert not pub_path.exists()

    info = describe("ssh")
    assert info["set"] is False


def test_talosconfig_is_described(setup_home) -> None:
    """A talosconfig maps contexts by name: describe gives its context and endpoints."""
    set_key("talos", "context: home\ncontexts:\n  home:\n    endpoints: [10.0.0.1, 10.0.0.2]\n  other:\n    endpoints: [10.9.9.9]\n")
    info = describe("talos")
    assert info["context"] == "home"
    assert info["endpoints"] == "10.0.0.1, 10.0.0.2"
    with pytest.raises(KeyError_, match="talosconfig"):
        set_key("talos", "contexts:\n- name: home\n")


def test_bad_yaml_and_bad_tokens_are_refused(setup_home) -> None:
    """Unparseable YAML and wrongly shaped tokens raise KeyError_, never anything else."""
    with pytest.raises(KeyError_, match="kubeconfig"):
        set_key("kubernetes", "contexts: [unclosed")
    with pytest.raises(KeyError_, match="app.terraform.io"):
        set_key("terraform", "short")
    with pytest.raises(KeyError_, match="API key"):
        set_key("local-llm", "has spaces in it, so no")
    with pytest.raises(KeyError_, match="Unknown"):
        set_key("nope", "x")
    with pytest.raises(KeyError_, match="Unknown"):
        remove_key("nope")


def test_github_without_gh_reads_as_unset(setup_home, monkeypatch) -> None:
    """A missing gh binary means GitHub shows as unset, and setting it fails with a KeyError_."""
    monkeypatch.setattr(keys, "GH", "/nonexistent/gh")
    assert describe("github")["set"] is False
    with pytest.raises(KeyError_):
        set_key("github", "ghp_good")
