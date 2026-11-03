"""Tests for scripts/next-version.py."""
import importlib.util
import sys
from pathlib import Path

import pytest

_path = Path(__file__).resolve().parent.parent / "scripts" / "next-version.py"
_spec = importlib.util.spec_from_file_location("next_version", _path)
nv = importlib.util.module_from_spec(_spec)
sys.modules["next_version"] = nv
_spec.loader.exec_module(nv)


def manifest(**tools: str) -> dict:
    """Build a manifest dict from tool name->version kwargs."""
    return {("tool", name.replace("_", "-")): version
            for name, version in tools.items()}


# ---------------------------------------------------------------------------
# Core logic tests
# ---------------------------------------------------------------------------

def test_first_release():
    """No previous release gives v1.0.0."""
    d = nv.decide(manifest(kubectl="1.36.6"), major=1)
    assert d.version == "v1.0.0"
    assert d.reason == "first release at major version 1"
    assert d.changes == []

    d2 = nv.decide(manifest(kubectl="1.36.6"), major=2)
    assert d2.version == "v2.0.0"


def test_nothing_changed():
    """Identical manifest with no inputs change gives no release."""
    prev = manifest(kubectl="1.36.6")
    curr = manifest(kubectl="1.36.6")
    d = nv.decide(curr, major=1, previous=prev, last_tag="v1.2.3")
    assert d.version is None
    assert d.reason == "no changes since v1.2.3"


def test_build_files_changed():
    """Identical manifest with inputs_changed True gives a patch bump."""
    prev = manifest(kubectl="1.36.6")
    curr = manifest(kubectl="1.36.6")
    d = nv.decide(curr, major=1, previous=prev, last_tag="v1.2.3",
                  inputs_changed=True)
    assert d.version == "v1.2.4"


def test_debian_change_is_patch():
    """Debian package changes are always patch."""
    prev = manifest(kubectl="1.36.6")
    prev[("deb", "bash")] = "5.2.21-2"
    curr = manifest(kubectl="1.36.6")
    curr[("deb", "bash")] = "5.2.21-3"
    d = nv.decide(curr, major=1, previous=prev, last_tag="v1.2.3")
    assert d.version == "v1.2.4"

    # Deb added: unlike a tool, never more than a patch.
    prev2 = manifest(kubectl="1.36.6")
    curr2 = manifest(kubectl="1.36.6")
    curr2[("deb", "libfoo1")] = "1.0-1"
    d2 = nv.decide(curr2, major=1, previous=prev2, last_tag="v1.2.3")
    assert d2.version == "v1.2.4"
    assert d2.reason == "patch: 1 Debian package change(s)"

    # Deb removed
    prev3 = manifest(kubectl="1.36.6")
    prev3[("deb", "bash")] = "5.2.21-2"
    curr3 = manifest(kubectl="1.36.6")
    d3 = nv.decide(curr3, major=1, previous=prev3, last_tag="v1.2.3")
    assert d3.version == "v1.2.4"


def test_build_files_named_alongside_other_changes():
    """The reason names changed build files even when a tool moved too."""
    prev = manifest(kubectl="1.36.6")
    curr = manifest(kubectl="1.36.7")
    d = nv.decide(curr, major=1, previous=prev, last_tag="v1.2.3",
                  inputs_changed=True)
    assert d.version == "v1.2.4"
    assert d.reason == "patch: 1 tool update(s), build files changed"


def test_tool_minor_update_is_patch():
    """kubectl 1.36.6 to 1.37.0 is a patch bump."""
    prev = manifest(kubectl="1.36.6")
    curr = manifest(kubectl="1.37.0")
    d = nv.decide(curr, major=1, previous=prev, last_tag="v1.2.3")
    assert d.version == "v1.2.4"


def test_tool_major_update_is_minor():
    """node 26.13.0 to 28.0.0 is a minor bump, resetting patch."""
    prev = manifest(node="26.13.0")
    curr = manifest(node="28.0.0")
    d = nv.decide(curr, major=1, previous=prev, last_tag="v1.2.3")
    assert d.version == "v1.3.0"
    assert d.reason == "minor: node (major update)"


def test_zero_x_minor_is_major():
    """For 0.x versions, the second number is the breaking one."""
    # 0.13.7 to 0.14.0 is a minor bump.
    prev = manifest(uv="0.13.7")
    curr = manifest(uv="0.14.0")
    d = nv.decide(curr, major=1, previous=prev, last_tag="v1.2.3")
    assert d.version == "v1.3.0"

    # 0.13.7 to 0.13.8 is a patch bump.
    curr2 = manifest(uv="0.13.8")
    d2 = nv.decide(curr2, major=1, previous=prev, last_tag="v1.2.3")
    assert d2.version == "v1.2.4"


def test_calendar_version_is_never_major():
    """Calendar versions (first number >= 1000) never count as major."""
    prev = manifest(mise="2026.10.25")
    curr = manifest(mise="2027.1.0")
    d = nv.decide(curr, major=1, previous=prev, last_tag="v1.2.3")
    assert d.version == "v1.2.4"


def test_switching_version_scheme_is_major():
    """Semantic to calendar versioning, or back, is a minor release."""
    for old, new in (("1.2.3", "2026.1.0"), ("2026.10.25", "1.0.0")):
        d = nv.decide(manifest(mise=new), major=1,
                      previous=manifest(mise=old), last_tag="v1.2.3")
        assert d.version == "v1.3.0", (old, new)


def test_tool_added_or_removed_is_minor():
    """Adding or removing a tool is a minor bump."""
    prev = manifest(node="26.13.0")
    curr = manifest(node="26.13.0", restic="0.17.0")
    d = nv.decide(curr, major=1, previous=prev, last_tag="v1.2.3")
    assert d.version == "v1.3.0"
    assert d.reason == "minor: restic (added)"

    # Removing
    prev2 = manifest(node="26.13.0", restic="0.17.0")
    curr2 = manifest(node="26.13.0")
    d2 = nv.decide(curr2, major=1, previous=prev2, last_tag="v1.2.3")
    assert d2.version == "v1.3.0"
    assert d2.reason == "minor: restic (removed)"


def test_biggest_change_wins():
    """When both minor and patch changes exist, minor wins."""
    prev = manifest(node="26.13.0")
    prev[("deb", "bash")] = "5.2.21-2"
    curr = manifest(node="28.0.0")
    curr[("deb", "bash")] = "5.2.21-3"
    d = nv.decide(curr, major=1, previous=prev, last_tag="v1.2.3")
    assert d.version == "v1.3.0"


def test_major_raised():
    """Raising MAJOR_VERSION gives vN.0.0 even with no other changes."""
    prev = manifest(kubectl="1.36.6")
    curr = manifest(kubectl="1.36.6")
    d = nv.decide(curr, major=2, previous=prev, last_tag="v1.2.3")
    assert d.version == "v2.0.0"


def test_major_lowered_is_an_error():
    """Lowering MAJOR_VERSION is an error."""
    prev = manifest(kubectl="1.36.6")
    curr = manifest(kubectl="1.36.6")
    with pytest.raises(nv.ReleaseError):
        nv.decide(curr, major=1, previous=prev, last_tag="v2.0.0")


def test_unreadable_version_counts_as_major():
    """An unreadable version is treated as a major change."""
    prev = manifest(tool_name="1.2.3")
    curr = manifest(tool_name="nightly")
    d = nv.decide(curr, major=1, previous=prev, last_tag="v1.2.3")
    assert d.version == "v1.3.0"


# ---------------------------------------------------------------------------
# version_numbers parametrize
# ---------------------------------------------------------------------------

@pytest.mark.parametrize(
    "version, expected",
    [
        ("1.36.6", (1, 36, 6)),
        ("v4.3.1+g1234", (4, 3, 1)),
        ("1:2.47.3-0+deb13u1", (2, 47, 3)),
        ("2026.10.25", (2026, 10, 25)),
        ("abc", None),
    ],
)
def test_version_numbers(version, expected):
    """version_numbers handles all documented examples."""
    assert nv.version_numbers(version) == expected


# ---------------------------------------------------------------------------
# Bad manifest lines
# ---------------------------------------------------------------------------

def test_bad_manifest_lines():
    """Malformed manifest lines raise ReleaseError."""
    # Two fields
    with pytest.raises(nv.ReleaseError):
        nv.parse_manifest("tool\tname\n", "f")

    # Unknown kind
    with pytest.raises(nv.ReleaseError):
        nv.parse_manifest("unknown\tname\t1.0\n", "f")

    # Duplicate tool
    with pytest.raises(nv.ReleaseError) as exc_info:
        nv.parse_manifest(
            "tool\tname\t1.0\ntool\tname\t2.0\n", "f"
        )
    assert ":2:" in str(exc_info.value)


def test_previous_without_tag_is_an_error():
    """Passing --previous without --last-tag is an error."""
    with pytest.raises(nv.ReleaseError):
        nv.decide(manifest(kubectl="1.36.6"), major=1,
                  previous=manifest(kubectl="1.36.6"), last_tag=None)


def test_bad_tag():
    """A tag without the leading 'v' is rejected."""
    with pytest.raises(nv.ReleaseError):
        nv.parse_tag("1.2.3")


# ---------------------------------------------------------------------------
# CLI tests
# ---------------------------------------------------------------------------

def test_main_prints_version_and_writes_notes(tmp_path, capsys):
    """main with a minor change prints version, reason, and writes notes."""
    prev_text = (
        "tool\tnode\t26.13.0\n"
        "deb\tbash\t5.2.21-2\n"
    )
    curr_text = (
        "tool\tnode\t28.0.0\n"
        "deb\tbash\t5.2.21-3\n"
    )
    prev_file = tmp_path / "prev.txt"
    curr_file = tmp_path / "curr.txt"
    notes_file = tmp_path / "notes.md"
    prev_file.write_text(prev_text)
    curr_file.write_text(curr_text)

    rc = nv.main([
        "--current", str(curr_file),
        "--previous", str(prev_file),
        "--last-tag", "v1.2.3",
        "--major", "1",
        "--notes", str(notes_file),
    ])
    assert rc == 0
    captured = capsys.readouterr()
    assert captured.out == "v1.3.0\n"
    assert captured.err.startswith("next-version.py: minor:")
    notes = notes_file.read_text()
    assert "| node | 26.13.0 | 28.0.0 (major) |" in notes
    assert "## Debian packages" in notes
    assert "## Everything in this image" in notes


def test_main_no_release_prints_nothing(tmp_path, capsys):
    """Identical manifests produce no stdout and no notes file."""
    text = "tool\tnode\t26.13.0\n"
    prev_file = tmp_path / "prev.txt"
    curr_file = tmp_path / "curr.txt"
    notes_file = tmp_path / "notes.md"
    prev_file.write_text(text)
    curr_file.write_text(text)

    rc = nv.main([
        "--current", str(curr_file),
        "--previous", str(prev_file),
        "--last-tag", "v1.2.3",
        "--major", "1",
        "--notes", str(notes_file),
    ])
    assert rc == 0
    captured = capsys.readouterr()
    assert captured.out == ""
    assert not notes_file.exists()


def test_main_bad_input_returns_2(tmp_path, capsys):
    """A malformed manifest returns exit code 2 with error on stderr."""
    curr_file = tmp_path / "curr.txt"
    curr_file.write_text("bad line\n")

    rc = nv.main([
        "--current", str(curr_file),
        "--major", "1",
    ])
    assert rc == 2
    captured = capsys.readouterr()
    assert "error:" in captured.err
