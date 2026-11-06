"""Fixed catalogue of write-only keys and an allowlist of environment settings.

The page is where the box's keys go in. A key can be set, replaced or removed,
but its value is never read back or shown: the page shows only what it
authenticates as. A page that could display keys would be one stolen browser
session away from giving them all up.

Each key has exactly one place on disk, at mode 0600, inside a 0700 directory,
written atomically.

The SSH key is generated on the box, and only its public half is ever shown,
so the private key never travels.

Environment settings come from an allowlist, because some variables silently
break Remote Control or replace its sign-in. Those are refused with a reason.

Pasted values are checked before saving, so a wrong paste fails at once rather
than on the next kubectl call.
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import threading
from pathlib import Path

import yaml

from opsbox.manager.sessions import _write_private


# By full path: PATH starts with ~/.local/bin, which lives in the volume, so
# anything planted there would otherwise be handed the GitHub token on stdin.
# Tests point these at fakes.
GH = "/usr/bin/gh"
SSH_KEYGEN = "/usr/bin/ssh-keygen"

# Two saves at once would each read the file, and the second write would
# drop the first one's change.
_ENV_LOCK = threading.Lock()


class KeyError_(ValueError):
    """A pasted key that can't be saved; the message says what to do."""


def home() -> Path:
    """Return the opsbox home directory, allowing test monkeypatching."""
    return Path(os.environ.get("HOME", "/home/opsbox"))


# A hint only where the label doesn't already say what to paste.
CATALOGUE: list[dict] = [
    {"id": "kubernetes", "label": "Kubernetes", "hint": ""},
    {"id": "talos", "label": "Talos", "hint": ""},
    {"id": "github", "label": "GitHub", "hint": "A fine-grained personal access token."},
    {"id": "terraform", "label": "Terraform registry", "hint": "An app.terraform.io API token."},
    {"id": "local-llm", "label": "Local LLM", "hint": ""},
    {"id": "ssh", "label": "SSH", "hint": ""},
]

PATHS: dict[str, str] = {
    "kubernetes": ".kube/config",
    "talos": ".talos/config",
    "terraform": ".terraform.d/credentials.tfrc.json",
    "local-llm": ".config/local-llm/key",
    "ssh": ".ssh/id_ed25519",
}


def set_key(key_id: str, value: str) -> None:
    """Save *value* as the key identified by *key_id*."""
    value = value.strip()
    if len(value) > 65536 or "\x00" in value:
        raise KeyError_("That's not a key: it is too long or holds a NUL.")

    if key_id in ("kubernetes", "talos"):
        # A kubeconfig lists its contexts; a talosconfig maps them by name.
        contexts = _load_yaml(value).get("contexts")
        if not contexts or not isinstance(contexts, list if key_id == "kubernetes" else dict):
            kind = "kubeconfig" if key_id == "kubernetes" else "talosconfig"
            raise KeyError_(f"That doesn't look like a {kind}: it has no contexts.")
        _write_private(home() / PATHS[key_id], value + "\n")

    elif key_id == "github":
        try:
            proc = subprocess.run(
                [GH, "auth", "login", "--hostname", "github.com", "--with-token"],
                input=value,
                text=True,
                capture_output=True,
                timeout=60,
            )
        except (OSError, subprocess.SubprocessError):
            raise KeyError_("GitHub couldn't be reached to check that token; try again.") from None
        if proc.returncode != 0:
            first_line = (proc.stderr.strip().splitlines() or ["no reason given"])[0]
            raise KeyError_(f"GitHub refused that token: {first_line}")

    elif key_id == "terraform":
        if not re.fullmatch(r"[A-Za-z0-9._-]{20,512}", value):
            raise KeyError_("That doesn't look like an app.terraform.io token.")
        payload = json.dumps({"credentials": {"app.terraform.io": {"token": value}}})
        _write_private(home() / PATHS[key_id], payload)

    elif key_id == "local-llm":
        if not re.fullmatch(r"[A-Za-z0-9._-]{16,512}", value):
            raise KeyError_("That doesn't look like an API key: it should be at least 16 letters, digits, dots, dashes or underscores.")
        key_path = home() / PATHS[key_id]
        _write_private(key_path, value + "\n")
        _write_env("LOCAL_LLM_KEY_FILE", str(key_path))

    elif key_id == "ssh":
        raise KeyError_("The SSH key is generated on the box: use Generate.")

    else:
        raise KeyError_("Unknown key.")


def _load_yaml(text: str) -> dict:
    """Parse a pasted config, giving {} for anything that isn't a YAML mapping."""
    try:
        parsed = yaml.safe_load(text)
    except yaml.YAMLError:
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _named(entries: object, name: object) -> dict:
    """Find the entry called *name* in a kubeconfig list, or {}."""
    for entry in entries if isinstance(entries, list) else []:
        if isinstance(entry, dict) and entry.get("name") == name:
            return entry
    return {}


def generate_ssh() -> None:
    """Generate an Ed25519 SSH key pair on the box."""
    key_path = home() / PATHS["ssh"]
    if key_path.exists():
        raise KeyError_("There is already an SSH key; remove it first.")
    ssh_dir = key_path.parent
    ssh_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
    try:
        subprocess.run(
            [SSH_KEYGEN, "-q", "-t", "ed25519", "-N", "", "-C", "opsbox", "-f", str(key_path)],
            stdin=subprocess.DEVNULL,
            capture_output=True,
            check=True,
            timeout=30,
        )
    except (OSError, subprocess.SubprocessError):
        raise KeyError_("ssh-keygen couldn't make the key.") from None


def remove_key(key_id: str) -> None:
    """Remove the key identified by *key_id*."""
    if key_id in PATHS:
        key_path = home() / PATHS[key_id]
        if key_path.exists():
            key_path.unlink()
        if key_id == "ssh":
            pub_path = key_path.with_suffix(".pub")
            if pub_path.exists():
                pub_path.unlink()
    elif key_id == "github":
        try:
            subprocess.run(
                [GH, "auth", "logout", "--hostname", "github.com"],
                capture_output=True,
                check=False,
                timeout=30,
            )
        except (OSError, subprocess.SubprocessError):
            raise KeyError_("gh couldn't sign out; try again.") from None
    else:
        raise KeyError_("Unknown key.")

    if key_id == "local-llm":
        _write_env("LOCAL_LLM_KEY_FILE", "")


def describe(key_id: str) -> dict:
    """Return non-secret fields for the key identified by *key_id*."""
    info: dict = {"id": key_id, "label": "", "hint": "", "set": False}

    for entry in CATALOGUE:
        if entry["id"] == key_id:
            info["label"] = entry["label"]
            info["hint"] = entry["hint"]
            break

    if key_id in PATHS:
        key_path = home() / PATHS[key_id]
        if key_path.exists():
            info["set"] = True
            info["updated"] = int(key_path.stat().st_mtime)

    if key_id in ("kubernetes", "talos") and info["set"]:
        try:
            data = yaml.safe_load((home() / PATHS[key_id]).read_text())
            current = data.get("context" if key_id == "talos" else "current-context", "")
            info["context"] = current
            if key_id == "talos":
                # A talosconfig keeps each context's endpoints in the context itself.
                endpoints = (data.get("contexts") or {}).get(current, {}).get("endpoints", [])
                info["endpoints"] = ", ".join(endpoints)
            else:
                # A kubeconfig's contexts, clusters and users are lists of named entries.
                ctx = _named(data.get("contexts"), current).get("context", {})
                cluster = _named(data.get("clusters"), ctx.get("cluster")).get("cluster", {})
                info["server"] = cluster.get("server", "")
                info["user"] = ctx.get("user", "")
        except Exception:
            info["problem"] = "unreadable"

    elif key_id == "github":
        # gh keeps its own token, so whether it's set is whatever gh says.
        try:
            proc = subprocess.run(
                [GH, "auth", "status", "--hostname", "github.com"],
                capture_output=True,
                text=True,
                timeout=30,
            )
        except (OSError, subprocess.SubprocessError):
            proc = None
        if proc is not None and proc.returncode == 0:
            info["set"] = True
            found = re.search(r"\baccount (\S+)", proc.stdout + proc.stderr)
            if found:
                info["account"] = found.group(1)

    elif key_id == "terraform" and info["set"]:
        info["host"] = "app.terraform.io"

    elif key_id == "ssh" and info["set"]:
        key_path = home() / PATHS["ssh"]
        pub_path = key_path.with_suffix(".pub")
        if pub_path.exists():
            info["public"] = pub_path.read_text().strip()
            try:
                fp_proc = subprocess.run(
                    [SSH_KEYGEN, "-lf", str(pub_path)],
                    capture_output=True,
                    text=True,
                    timeout=30,
                )
                fields = fp_proc.stdout.split()
            except (OSError, subprocess.SubprocessError):
                fields = []
            if len(fields) >= 2:
                info["fingerprint"] = fields[1]

    return info


def describe_all() -> list[dict]:
    """Return describe() for every entry in the catalogue."""
    return [describe(entry["id"]) for entry in CATALOGUE]


ENV_ALLOWED: dict[str, str] = {
    "CLAUDE_REMOTE_CONTROL_SESSION_NAME_PREFIX": "The prefix of each session's name in the Claude app.",
    "DOCKER_HOST": "Where the docker CLI builds and runs containers.",
    "TZ": "The timezone for logs and local times.",
    "GIT_AUTHOR_NAME": "The name on commits.",
    "GIT_AUTHOR_EMAIL": "The email on commits.",
    "HTTPS_PROXY": "A proxy for outbound HTTPS.",
    "NO_PROXY": "Hosts that skip the proxy.",
}

ENV_REFUSED: dict[str, str] = {
    "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC": "It turns Remote Control off.",
    "DISABLE_GROWTHBOOK": "It turns Remote Control off.",
    "ANTHROPIC_API_KEY": "Remote Control needs a claude.ai sign-in, not an API key.",
    "ANTHROPIC_AUTH_TOKEN": "Remote Control needs a claude.ai sign-in, not a token.",
    "CLAUDE_CODE_OAUTH_TOKEN": "Remote Control needs a claude.ai sign-in, not a setup token.",
}


def env_path() -> Path:
    """Return the path to the environment settings file."""
    return home() / ".config" / "opsbox" / "env"


def get_env() -> dict[str, str]:
    """Parse and return the environment settings file as a dict."""
    result: dict[str, str] = {}
    try:
        for line in env_path().read_text().splitlines():
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            if "=" in line:
                name, value = line.split("=", 1)
                result[name] = value
    except FileNotFoundError:
        pass
    return result


def set_env(name: str, value: str) -> None:
    """Set an allowed environment variable, refusing unknown or dangerous names."""
    if name in ENV_REFUSED:
        raise KeyError_(f"{name}: {ENV_REFUSED[name]}")
    if name not in ENV_ALLOWED:
        raise KeyError_(f"{name} isn't a setting the manager manages.")
    if "\n" in value or "\r" in value or "\x00" in value or len(value) > 1024:
        raise KeyError_("That value has a line break, a NUL or is too long.")

    _write_env(name, value)


def _write_env(name: str, value: str) -> None:
    """Set or (when *value* is empty) remove one name, keeping every other line, managed names included."""
    with _ENV_LOCK:
        env_file = env_path()
        lines = env_file.read_text().splitlines() if env_file.exists() else []
        lines = [line for line in lines if not line.startswith(name + "=")]
        if value:
            lines.append(f"{name}={value}")
        _write_private(env_file, "".join(line + "\n" for line in lines))
