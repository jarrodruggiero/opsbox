"""Files a person uploads to a path on the box: a kubeconfig, a talosconfig, a folder of either.

The page never shows a file's contents, only its path, size and date. Everything
it writes is private (0600 in 0700 directories) and written atomically.

**A path is only ever under the box's home**, and not through a symbolic link,
because the person naming it is typing into a form, and a mistake shouldn't reach
the rest of the disk. (It guards against mistakes, not against something already
running as this user, which could swap a directory after the check, and could
just as well write there itself.) It can't overwrite the manager's own files
either: the owner, the sessions and the settings each have their own rules, which
a file dropped over them would walk past.

Only a file on the page's list can be removed, so Remove can't be turned into a
way to delete anything else in the volume. The list is what the page saved, and
starts with what the first manager's keys page left at its four paths.
"""
from __future__ import annotations

import json
import threading
import time
from pathlib import Path

from opsbox.manager.keys import Refused, home
from opsbox.manager.sessions import _write_private

MAX_FILE = 1048576
MAX_PATH = 255

# The manager's owner file, sessions, CSRF secret, settings and this list.
OFF_LIMITS = ".config/opsbox"

# Where the keys page of the first manager kept things, so a box that used it
# still lists them (and can replace or remove them).
EARLIER_PATHS = (".kube/config", ".talos/config", ".terraform.d/credentials.tfrc.json", ".config/local-llm/key")

_LOCK = threading.Lock()


def _manifest_path() -> Path:
    return home() / ".config/opsbox/files.json"


def _read_manifest() -> dict[str, dict]:
    """The files the page has saved, by path relative to home."""
    try:
        data = json.loads(_manifest_path().read_text())
    except FileNotFoundError:
        return _earlier_files()
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def _earlier_files() -> dict[str, dict]:
    """What the first manager's keys page left, before there was a list."""
    found = {}
    for relative in EARLIER_PATHS:
        path = home() / relative
        if path.is_file() and not path.is_symlink():
            stat = path.stat()
            found[relative] = {"size": stat.st_size, "uploaded": int(stat.st_mtime)}
    return found


def relative_path(raw: str) -> str:
    """Turn what was typed (~/x, /home/opsbox/x, or x) into a checked path relative to home."""
    text = raw.strip()
    if not text:
        raise Refused("Say where the file goes, such as ~/.kube/config.")
    if len(text) > MAX_PATH or any(ord(c) < 32 or ord(c) == 127 for c in text):
        raise Refused("That path is too long or has characters a file name can't have.")
    if text == "~" or text.endswith("/"):
        raise Refused("That's a directory: add the file's name.")
    if text.startswith("~/"):
        text = text[2:]
    elif text.startswith("$HOME/"):
        text = text[6:]
    elif text.startswith("/"):
        prefix = f"{home()}/"
        if not text.startswith(prefix):
            raise Refused("The path must be under the box's home directory (~).")
        text = text[len(prefix):]
    elif text.startswith("~"):
        raise Refused("The path must be under the box's home directory (~).")
    parts = text.split("/")
    if any(part in ("", ".", "..") for part in parts):
        raise Refused("A path can't contain empty parts, . or ..")
    # Counted in bytes, as the filesystem counts: past 255, even asking whether it
    # exists raises.
    if any(len(part.encode()) > 255 for part in parts):
        raise Refused("That path is too long or has characters a file name can't have.")
    relative = "/".join(parts)
    if relative == OFF_LIMITS or relative.startswith(f"{OFF_LIMITS}/"):
        raise Refused("That's where the manager keeps its own files.")
    current = home()
    state = home() / OFF_LIMITS
    for part in parts:
        current = current / part
        if current.is_symlink():
            raise Refused("That path goes through a symbolic link, so the file could land anywhere.")
        # By identity too: a case-insensitive filesystem has .CONFIG/opsbox as well.
        if current.exists() and state.exists() and current.samefile(state):
            raise Refused("That's where the manager keeps its own files.")
    if current.is_dir():
        raise Refused("That's a directory: add the file's name.")
    return relative


def _make_parents(path: Path) -> None:
    """Create the missing directories one at a time, each private: makedirs' mode only reaches the last."""
    missing = []
    parent = path.parent
    while not parent.exists():
        missing.append(parent)
        parent = parent.parent
    if not parent.is_dir():
        raise Refused("Part of that path is a file, not a directory.")
    for directory in reversed(missing):
        directory.mkdir(mode=0o700)


def save(raw_path: str, data: bytes, *, from_folder: bool = False) -> str:
    """Save an uploaded file at a checked path, and return that path as the page shows it.

    An empty file is refused: it's more likely a mistake that would replace a good
    one. From a folder it's part of what was chosen, so it's kept, but only as a
    new file or over one that's empty already, so that nothing can be lost to it.
    """
    if len(data) > MAX_FILE:
        raise Refused(f"That file is over the {MAX_FILE // 1048576} MiB limit.")
    relative = relative_path(raw_path)
    path = home() / relative
    if not data and (not from_folder or (path.is_file() and path.stat().st_size)):
        raise Refused("That file is empty.")
    try:
        _make_parents(path)
        _write_private(path, data)
    except OSError as failed:
        raise Refused(f"The box couldn't save that file: {failed.strerror}.") from None
    with _LOCK:
        manifest = _read_manifest()
        manifest[relative] = {"size": len(data), "uploaded": int(time.time())}
        _write_private(_manifest_path(), json.dumps(manifest))
    return f"~/{relative}"


def remove(raw_path: str) -> None:
    """Delete a file the page saved; anything else is refused."""
    relative = relative_path(raw_path)
    with _LOCK:
        manifest = _read_manifest()
        if relative not in manifest:
            raise Refused("The page only removes files it saved.")
        (home() / relative).unlink(missing_ok=True)
        del manifest[relative]
        _write_private(_manifest_path(), json.dumps(manifest))


def listing() -> list[dict]:
    """The saved files that still exist, sorted by path: never their contents."""
    with _LOCK:
        manifest = _read_manifest()
    rows = []
    for relative in sorted(manifest):
        # The list is a file in the volume: what's on it gets the same checks as
        # anything typed, or it could name a file outside home.
        try:
            relative_path(relative)
        except Refused:
            continue
        path = home() / relative
        entry = manifest[relative]
        if path.is_file() and not path.is_symlink() and isinstance(entry, dict):
            uploaded = entry.get("uploaded")
            rows.append({"path": f"~/{relative}", "size": path.stat().st_size, "uploaded": uploaded if isinstance(uploaded, int) else 0})
    return rows
