"""Tests for uploading config files to a path on the box."""
from __future__ import annotations

import pathlib

import pytest

from opsbox.manager import files
from opsbox.manager.keys import Refused


@pytest.fixture(autouse=True)
def home(tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch) -> pathlib.Path:
    """HOME is a scratch directory, with a file outside it for a traversal to reach."""
    directory = tmp_path / "home"
    directory.mkdir()
    (tmp_path / "outside").write_text("outside")
    monkeypatch.setenv("HOME", str(directory))
    return directory


@pytest.mark.parametrize("typed", ["~/.kube/config", ".kube/config", "$HOME/.kube/config", "  ~/.kube/config  "])
def test_every_way_of_saying_a_path_lands_in_the_same_place(typed: str, home: pathlib.Path) -> None:
    assert files.save(typed, b"data") == "~/.kube/config"
    assert (home / ".kube/config").read_bytes() == b"data"


def test_an_absolute_path_under_home_is_fine(home: pathlib.Path) -> None:
    assert files.save(f"{home}/x/y", b"data") == "~/x/y"


def test_a_saved_file_and_its_new_directories_are_private(home: pathlib.Path) -> None:
    """Keys go here: every directory created, not just the last, and the file itself."""
    files.save("~/a/b/c/key", b"data")
    assert (home / "a/b/c/key").stat().st_mode & 0o777 == 0o600
    for directory in ("a", "a/b", "a/b/c"):
        assert (home / directory).stat().st_mode & 0o777 == 0o700, directory


def test_bytes_arrive_exactly_as_sent(home: pathlib.Path) -> None:
    every_byte = bytes(range(256)) + b"\r\n\r\n--x--\r\n"
    files.save("~/binary", every_byte)
    assert (home / "binary").read_bytes() == every_byte


@pytest.mark.parametrize("typed", [
    "/etc/passwd",
    "~/../outside",
    "../outside",
    "a/../../outside",
    "~other/x",
    "..",
    "a/./b",
    "a//b",
])
def test_nothing_outside_home_can_be_named(typed: str, home: pathlib.Path) -> None:
    """A mistake or a stolen session stays in the volume."""
    with pytest.raises(Refused):
        files.save(typed, b"data")
    assert (home.parent / "outside").read_text() == "outside"


@pytest.mark.parametrize("typed", [".config/opsbox/env", ".config/opsbox/manager/owner", "~/.config/opsbox/manager/sessions.json", "~/.config/opsbox"])
def test_the_managers_own_files_are_off_limits(typed: str, home: pathlib.Path) -> None:
    """Dropping a file over these would walk past the owner pin, the sessions and the settings' rules."""
    with pytest.raises(Refused, match="its own files"):
        files.save(typed, b"data")
    assert not (home / ".config/opsbox").exists()


def test_another_spelling_of_the_managers_directory_is_off_limits_too(home: pathlib.Path) -> None:
    """On a case-insensitive filesystem .CONFIG/opsbox is the same directory, and a text comparison can't see that."""
    state = home / ".config/opsbox"
    state.mkdir(parents=True)
    try:
        files.save("~/.CONFIG/opsbox/owner", b"data")
    except Refused:
        pass
    assert not (state / "owner").exists()


def test_a_symbolic_link_is_never_followed(home: pathlib.Path) -> None:
    """Whether it's the file or a directory on the way, a link could lead anywhere."""
    (home / "linkdir").symlink_to(home.parent)
    (home / "linkfile").symlink_to(home.parent / "outside")
    for typed, _where in (("~/linkdir/outside", "directory"), ("~/linkfile", "file")):
        with pytest.raises(Refused, match="symbolic link"):
            files.save(typed, b"data")
    assert (home.parent / "outside").read_text() == "outside"


@pytest.mark.parametrize("typed, message", [
    ("", "Say where"),
    ("   ", "Say where"),
    ("~", "directory"),
    ("~/somewhere/", "directory"),
    ("a\nb", "characters"),
    ("a\x00b", "characters"),
    ("x" * 300, "too long"),
])
def test_paths_that_cant_be_a_file_are_refused(typed: str, message: str) -> None:
    with pytest.raises(Refused, match=message):
        files.save(typed, b"data")


def test_an_existing_directory_isnt_replaced(home: pathlib.Path) -> None:
    (home / "dir").mkdir()
    (home / "dir/inside").write_text("kept")
    with pytest.raises(Refused, match="directory"):
        files.save("~/dir", b"data")
    assert (home / "dir/inside").read_text() == "kept"


def test_a_file_in_the_way_of_a_directory_is_refused(home: pathlib.Path) -> None:
    (home / "plain").write_text("kept")
    with pytest.raises(Refused, match="is a file"):
        files.save("~/plain/under", b"data")
    assert (home / "plain").read_text() == "kept"


def test_too_big_a_file_is_refused(home: pathlib.Path) -> None:
    with pytest.raises(Refused, match="limit"):
        files.save("~/big", b"x" * (files.MAX_FILE + 1))
    assert not (home / "big").exists()


def test_the_listing_shows_path_size_and_date_never_contents() -> None:
    files.save("~/.talos/config", b"SENTINEL-7f3a")
    files.save("~/.kube/config", b"longer SENTINEL-7f3a")
    rows = files.listing()
    assert [row["path"] for row in rows] == ["~/.kube/config", "~/.talos/config"]
    assert rows[1]["size"] == len(b"SENTINEL-7f3a") and rows[1]["uploaded"] > 0
    assert "SENTINEL-7f3a" not in str(rows)


def test_a_file_deleted_elsewhere_leaves_the_listing(home: pathlib.Path) -> None:
    """A session can delete it from a shell: the page shouldn't offer what isn't there."""
    files.save("~/gone", b"data")
    (home / "gone").unlink()
    assert files.listing() == []


def test_replacing_keeps_one_row(home: pathlib.Path) -> None:
    files.save("~/x", b"one")
    files.save("x", b"three")
    assert [(row["path"], row["size"]) for row in files.listing()] == [("~/x", 5)]
    assert (home / "x").read_bytes() == b"three"


def test_only_files_the_page_saved_can_be_removed(home: pathlib.Path) -> None:
    """Remove must not become a way to delete anything in the volume, such as Claude's sign-in."""
    (home / ".claude.json").write_text("{}")
    with pytest.raises(Refused, match="only removes files it saved"):
        files.remove("~/.claude.json")
    assert (home / ".claude.json").exists()

    files.save("~/mine", b"data")
    files.remove("~/mine")
    assert not (home / "mine").exists()
    assert files.listing() == []


@pytest.mark.parametrize("relative", files.EARLIER_PATHS)
def test_what_the_first_managers_keys_page_saved_is_still_listed(relative: str, home: pathlib.Path) -> None:
    """A box that used the first manager can still see, replace and remove what it put there."""
    (home / relative).parent.mkdir(parents=True)
    (home / relative).write_text("from the first manager")
    assert [row["path"] for row in files.listing()] == [f"~/{relative}"]
    files.save("~/new", b"data")
    assert [row["path"] for row in files.listing()] == sorted([f"~/{relative}", "~/new"])
    files.remove(f"~/{relative}")
    assert not (home / relative).exists()


def test_a_poisoned_list_cant_break_the_page_or_widen_remove(home: pathlib.Path) -> None:
    """The list is a file in the volume: odd entries are ignored, and a path outside home stays refused."""
    (home / ".config/opsbox").mkdir(parents=True)
    (home / "real").write_text("kept")
    (home / ".config/opsbox/files.json").write_text('{"real": {"uploaded": "not a date"}, "gone": 7, "../outside": {}}')
    rows = files.listing()
    assert [row["path"] for row in rows] == ["~/real"] and rows[0]["uploaded"] == 0
    with pytest.raises(Refused):
        files.remove("~/../outside")
    assert (home.parent / "outside").read_text() == "outside"
