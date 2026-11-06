"""The box's SSH key and its environment settings.

The SSH key is generated on the box and only its public half is ever shown, so
the private key never travels.

Settings are free-form: a person names them and gives them a value. The few that
would silently break Remote Control or replace its sign-in are refused with the
reason. A setting's value can be a token, so one can be hidden: it's then never
sent to a browser again, only replaced or removed. That keeps it off the screen
and out of the page. It isn't a wall against a stolen session, which could open
a shell on the box and read it there.
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import threading
from pathlib import Path

from opsbox.manager.sessions import _write_private

# By full path: PATH starts with ~/.local/bin, which lives in the volume.
SSH_KEYGEN = "/usr/bin/ssh-keygen"

# Two saves at once would each read the file, and the second write would drop
# the first one's change.
_ENV_LOCK = threading.Lock()

ENV_REFUSED: dict[str, str] = {
    "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC": "It turns Remote Control off.",
    "DISABLE_GROWTHBOOK": "It turns Remote Control off.",
    "ANTHROPIC_API_KEY": "Remote Control needs a claude.ai sign-in, not an API key.",
    "ANTHROPIC_AUTH_TOKEN": "Remote Control needs a claude.ai sign-in, not a token.",
    "CLAUDE_CODE_OAUTH_TOKEN": "Remote Control needs a claude.ai sign-in, not a setup token.",
    "CLAUDE_CONFIG_DIR": "It moves Claude Code away from the box's sign-in.",
    "HOME": "Everything on the box, its sign-in included, lives under it.",
}

ENV_NAME = re.compile(r"[A-Za-z_][A-Za-z0-9_]{0,127}")
MAX_ENV_VALUE = 4096

# Line breaks by anyone's definition, not only \n: str.splitlines and bash disagree
# about the rest, and a value that one reader splits is a forged setting to it.
UNSAFE_IN_VALUE = re.compile(r"[\x00-\x08\x0a-\x1f\x7f\x85\u2028\u2029]")

# Hidden whether or not the box was ticked: a name that says it's a secret, or a
# URL carrying one (a proxy's password, a token as an https user). Not ssh://me@box,
# where the user is just a user.
SECRET_NAME = re.compile(r"token|secret|passw|passphrase|credential|private|api_?key|jwt|_key$|^key$|_pat$|_pass$|pwd$|auth$", re.IGNORECASE)
SECRET_IN_URL = re.compile(r"://[^/@\s]*:[^/@\s]+@|https?://[^/@\s]+@", re.IGNORECASE)


class Refused(ValueError):
    """Something the page was asked to do that it won't; the message says what to do."""


def home() -> Path:
    """The box's home directory (read each time, so tests can move it)."""
    return Path(os.environ.get("HOME", "/home/opsbox"))


def ssh_key_path() -> Path:
    """Where the generated private key lives."""
    return home() / ".ssh/id_ed25519"


def generate_ssh() -> None:
    """Generate an Ed25519 key pair on the box."""
    path = ssh_key_path()
    if path.exists():
        raise Refused("There is already an SSH key; remove it first.")
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    try:
        subprocess.run(
            [SSH_KEYGEN, "-q", "-t", "ed25519", "-N", "", "-C", "opsbox", "-f", str(path)],
            stdin=subprocess.DEVNULL,
            capture_output=True,
            check=True,
            timeout=30,
        )
    except (OSError, subprocess.SubprocessError):
        raise Refused("ssh-keygen couldn't make the key.") from None


def remove_ssh() -> None:
    """Delete the key pair."""
    path = ssh_key_path()
    path.unlink(missing_ok=True)
    path.with_suffix(".pub").unlink(missing_ok=True)


def describe_ssh() -> dict:
    """Whether there is a key, and its public half and fingerprint: never the private half."""
    info: dict = {"set": ssh_key_path().exists()}
    public = ssh_key_path().with_suffix(".pub")
    if not info["set"]:
        return info
    # Anything can be uploaded here, and one odd file mustn't take the whole page down.
    try:
        info["public"] = public.read_text(errors="replace").strip()
        fields = subprocess.run(
            [SSH_KEYGEN, "-lf", str(public)], capture_output=True, text=True, errors="replace", timeout=30
        ).stdout.split()
    except (OSError, subprocess.SubprocessError):
        return info
    if len(fields) >= 2:
        info["fingerprint"] = fields[1]
    return info


def env_path() -> Path:
    """The settings file: NAME=VALUE lines, read by the supervisor and by shells."""
    return home() / ".config/opsbox/env"


def _hidden_path() -> Path:
    return home() / ".config/opsbox/env-hidden.json"


def _parse_env(text: str) -> dict[str, str]:
    """NAME=VALUE lines; anything else in the file is ignored. Only \\n ends a line, as for bash."""
    result: dict[str, str] = {}
    for line in text.split("\n"):
        name, separator, value = line.partition("=")
        if separator and ENV_NAME.fullmatch(name):
            result[name] = value.strip()
    return result


def read_env_file(path: Path) -> dict[str, str]:
    """The settings a process starts with: what the file says, minus anything the page refuses.

    At the point of use, because the file can be edited by hand from a shell, and
    a sign-in-replacing variable would stop Remote Control starting without a word.
    """
    try:
        env = _parse_env(path.read_text(errors="replace"))
    except OSError:
        return {}
    return {name: value for name, value in env.items() if name not in ENV_REFUSED}


def _read_env() -> dict[str, str]:
    """Everything in the file, so even a setting the page would refuse can be seen and removed."""
    try:
        return _parse_env(env_path().read_text(errors="replace"))
    except OSError:
        return {}


def _read_hidden() -> set[str] | None:
    """The names whose values the page never sends; None if the list can't be read."""
    try:
        names = json.loads(_hidden_path().read_text())
    except FileNotFoundError:
        return set()
    except (OSError, ValueError):
        return None
    return {name for name in names if isinstance(name, str)} if isinstance(names, list) else None


def _looks_secret(name: str, value: str) -> bool:
    """Whether a setting should be hidden whatever anyone ticked."""
    return bool(SECRET_NAME.search(name) or SECRET_IN_URL.search(value))


def _save(env: dict[str, str], hidden: set[str]) -> None:
    """Write both files; the hidden list first, so a crash between them hides too much, not too little."""
    _write_private(_hidden_path(), json.dumps(sorted(hidden & env.keys())))
    _write_private(env_path(), "".join(f"{name}={value}\n" for name, value in env.items()))


def env_rows() -> list[dict]:
    """Every setting for the page, sorted by name, with hidden values left out.

    Decided here as well as when saving: the file may have been written by an
    older version or by hand, and a list that can't be read hides everything.
    """
    env = _read_env()
    listed = _read_hidden()
    rows = []
    for name in sorted(env):
        hidden = listed is None or name in listed or _looks_secret(name, env[name])
        rows.append({"name": name, "hidden": hidden, "value": None if hidden else env[name]})
    return rows


def set_env(name: str, value: str, *, hidden: bool = False) -> None:
    """Save a setting, refusing a bad name or value with the reason.

    Once hidden it stays hidden: a rotated token saved without ticking the box
    mustn't appear on the page. To show one again, remove it and add it back.
    """
    if not ENV_NAME.fullmatch(name):
        raise Refused("A setting's name is letters, digits and underscores, and doesn't start with a digit.")
    if name in ENV_REFUSED:
        raise Refused(f"{name}: {ENV_REFUSED[name]}")
    value = value.strip()
    if not value:
        raise Refused("Give it a value, or use Remove.")
    if UNSAFE_IN_VALUE.search(value) or len(value) > MAX_ENV_VALUE:
        raise Refused("That value has a line break or a control character, or is too long.")
    with _ENV_LOCK:
        env = _read_env()
        listed = _read_hidden()
        # A list that couldn't be read means everything already there stays hidden.
        names = set(env) if listed is None else listed
        env[name] = value
        if hidden or _looks_secret(name, value):
            names.add(name)
        _save(env, names)


def remove_env(name: str) -> None:
    """Remove a setting, whatever put it there."""
    with _ENV_LOCK:
        env = _read_env()
        env.pop(name, None)
        listed = _read_hidden()
        _save(env, set(env) if listed is None else listed)
