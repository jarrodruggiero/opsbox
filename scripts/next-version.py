#!/usr/bin/env python3
"""Decide the next release version of the opsbox image.

usage: next-version.py --current FILE --major N
                       [--previous FILE --last-tag vX.Y.Z]
                       [--inputs-changed] [--notes FILE]

FILE is an image's /usr/local/share/opsbox/versions.txt: one line per tool
and per Debian package, tab-separated kind<TAB>name<TAB>version, where kind
is "tool" or "deb". --current is the candidate's, --previous the last
release's, and N the number in MAJOR_VERSION. plan-release.sh gathers all
of these; this script only decides, so it needs no git, Docker or network.

The rules, in order:

1. No previous release: vN.0.0.
2. N above the last release's major: vN.0.0. Below it: an error.
3. Otherwise the biggest of these:
   MINOR: a tool added or removed, or a tool's major version changed.
   PATCH: any other tool version change, any Debian package added,
   removed or changed, or --inputs-changed (the image's build files
   changed since the last release). MINOR resets the patch number to 0.
   Nothing changed: no release.
4. A tool's major version is its first number, except that for 0.x
   versions it is the second (uv 0.13 to 0.14 is the breaking step), and a
   calendar version (a first number of 1000 or more, like mise's
   2026.10.25) never has one: its year rolling over breaks nothing. A
   version that can't be read counts as a major change, because guessing
   big is safe and guessing small could ship a break as a patch.

It prints the next version, with its leading v, on stdout, or nothing when
no release is due, and one line on stderr saying why. With --notes, when a
release is due, it writes the release notes as Markdown to FILE.
"""
from __future__ import annotations

import argparse
import dataclasses
import re
import sys
from pathlib import Path

# ---------------------------------------------------------------------------
# Constants and types
# ---------------------------------------------------------------------------

# (kind, name) to version.
Manifest = dict[tuple[str, str], str]

KINDS = ("tool", "deb")

TAG = re.compile(r"v(\d+)\.(\d+)\.(\d+)")

NUMBERS = re.compile(r"\d+(?:\.\d+)*")


# ---------------------------------------------------------------------------
# Exceptions
# ---------------------------------------------------------------------------

class ReleaseError(Exception):
    """Bad input."""


# ---------------------------------------------------------------------------
# Manifest parsing
# ---------------------------------------------------------------------------

def parse_manifest(text: str, source: str) -> Manifest:
    """Parse a versions.txt file into a manifest dict."""
    result: Manifest = {}
    for lineno, line in enumerate(text.splitlines(), start=1):
        if not line.strip():
            continue
        parts = line.split("\t")
        if len(parts) != 3 or not all(p.strip() for p in parts):
            raise ReleaseError(
                f"{source}:{lineno}: expected kind<TAB>name<TAB>version, "
                f"got {line!r}"
            )
        kind, name, version = (p.strip() for p in parts)
        if kind not in KINDS:
            raise ReleaseError(
                f"{source}:{lineno}: expected kind<TAB>name<TAB>version, "
                f"got {line!r}"
            )
        if (kind, name) in result:
            raise ReleaseError(
                f"{source}:{lineno}: duplicate {name}"
            )
        result[(kind, name)] = version
    return result


# ---------------------------------------------------------------------------
# Version helpers
# ---------------------------------------------------------------------------

def version_numbers(version: str) -> tuple[int, ...] | None:
    """Return the numeric components of a version string, or None."""
    v = version
    # Strip epoch if present (e.g. "1:2.47.3-0+deb13u1").
    epoch_match = re.match(r"^\d+:", v)
    if epoch_match:
        v = v[epoch_match.end():]
    # Strip leading 'v'.
    if v.startswith("v"):
        v = v[1:]
    m = NUMBERS.match(v)
    if not m:
        return None
    return tuple(int(x) for x in m.group(0).split("."))


def is_major_change(old: str | None, new: str | None) -> bool:
    """Decide whether a tool version bump is major.

    Guessing big is safe; guessing small could ship a break as a patch.
    Unreadable versions count as major.
    """
    # Unreadable versions are treated as a major change.
    if old is None or new is None:
        return True
    old_nums = version_numbers(old)
    new_nums = version_numbers(new)
    # If either is unreadable, treat as major.
    if old_nums is None or new_nums is None:
        return True
    # Calendar version (first number >= 1000): year rolling over is not a
    # break, so never major.
    if old_nums[0] >= 1000 or new_nums[0] >= 1000:
        return False
    # 0.x versions: the second number is the breaking one.
    # A missing second number counts as 0.
    if old_nums[0] == 0 and new_nums[0] == 0:
        old_second = old_nums[1] if len(old_nums) > 1 else 0
        new_second = new_nums[1] if len(new_nums) > 1 else 0
        return old_second != new_second
    # Otherwise compare the first number.
    return old_nums[0] != new_nums[0]


# ---------------------------------------------------------------------------
# Change tracking
# ---------------------------------------------------------------------------

@dataclasses.dataclass(frozen=True)
class Change:
    """A single change between two manifests."""
    kind: str
    name: str
    old: str | None
    new: str | None

    def bump(self) -> str:
        """Return 'minor' or 'patch' per the release rules."""
        # Debian stable ships no major updates, so any change to a package is
        # a patch: a security fix, or a library an upgrade pulled in or
        # dropped.
        if self.kind == "deb":
            return "patch"
        # A tool added or removed can break a script that uses it.
        if self.old is None or self.new is None:
            return "minor"
        if is_major_change(self.old, self.new):
            return "minor"
        return "patch"

    def describe(self) -> str:
        """Return a human-readable description of the change."""
        if self.old is None:
            return "added"
        if self.new is None:
            return "removed"
        if is_major_change(self.old, self.new):
            return "major update"
        return "update"


def diff(previous: Manifest, current: Manifest) -> list[Change]:
    """Return all changes between two manifests, sorted by (kind, name)."""
    changes: list[Change] = []
    all_keys = sorted(set(previous.keys()) | set(current.keys()))
    for key in all_keys:
        old = previous.get(key)
        new = current.get(key)
        if old == new:
            continue
        changes.append(Change(kind=key[0], name=key[1], old=old, new=new))
    return changes


# ---------------------------------------------------------------------------
# Tag parsing
# ---------------------------------------------------------------------------

def parse_tag(tag: str) -> tuple[int, int, int]:
    """Parse a vX.Y.Z tag into (major, minor, patch)."""
    m = TAG.fullmatch(tag)
    if not m:
        raise ReleaseError(f"not a vX.Y.Z tag: {tag!r}")
    return (int(m.group(1)), int(m.group(2)), int(m.group(3)))


# ---------------------------------------------------------------------------
# Decision
# ---------------------------------------------------------------------------

@dataclasses.dataclass(frozen=True)
class Decision:
    """The result of deciding the next version."""
    version: str | None
    reason: str
    changes: list[Change] = dataclasses.field(default_factory=list)


def decide(
    current: Manifest,
    major: int,
    previous: Manifest | None = None,
    last_tag: str | None = None,
    inputs_changed: bool = False,
) -> Decision:
    """Apply the release rules and return a Decision."""
    if (previous is None) != (last_tag is None):
        raise ReleaseError("--previous and --last-tag go together")
    if major < 1:
        raise ReleaseError("--major must be >= 1")

    # Rule 1: no previous release.
    if previous is None:
        return Decision(
            version=f"v{major}.0.0",
            reason=f"first release at major version {major}",
            changes=[],
        )

    # Parse the last tag to get its major version.
    last_major, last_minor, last_patch = parse_tag(last_tag)

    # Rule 2: major version changed.
    if major > last_major:
        return Decision(
            version=f"v{major}.0.0",
            reason=f"MAJOR_VERSION is {major}, above {last_tag}",
            changes=[],
        )
    if major < last_major:
        raise ReleaseError(
            f"MAJOR_VERSION is {major}, below {last_tag}"
        )

    # Rule 3: find the biggest change.
    changes = diff(previous, current)

    # Classify changes.
    minor_changes = [c for c in changes if c.bump() == "minor"]
    patch_changes = [c for c in changes if c.bump() == "patch"]

    if minor_changes:
        # Build reason string for minor.
        parts: list[str] = []
        for c in minor_changes:
            parts.append(f"{c.name} ({c.describe()})")
        reason = f"minor: {', '.join(parts)}"
        return Decision(
            version=f"v{major}.{last_minor + 1}.0",
            reason=reason,
            changes=changes,
        )

    if patch_changes or inputs_changed:
        parts = []
        if patch_changes:
            tool_patches = [c for c in patch_changes if c.kind == "tool"]
            deb_patches = [c for c in patch_changes if c.kind == "deb"]
            if tool_patches:
                parts.append(f"{len(tool_patches)} tool update(s)")
            if deb_patches:
                parts.append(f"{len(deb_patches)} Debian package change(s)")
        if inputs_changed:
            parts.append("build files changed")
        reason = f"patch: {', '.join(parts)}"
        return Decision(
            version=f"v{major}.{last_minor}.{last_patch + 1}",
            reason=reason,
            changes=changes,
        )

    # Nothing changed.
    return Decision(
        version=None,
        reason=f"no changes since {last_tag}",
        changes=changes,
    )


# ---------------------------------------------------------------------------
# Release notes
# ---------------------------------------------------------------------------

def render_notes(decision: Decision, current: Manifest) -> str:
    """Render release notes as Markdown."""
    lines: list[str] = []

    # Reason paragraph.
    reason = decision.reason
    lines.append(reason[0].upper() + reason[1:] + ".")
    lines.append("")

    # Tool changes table.
    tool_changes = [c for c in decision.changes if c.kind == "tool"]
    if tool_changes:
        lines.append("## Tools")
        lines.append("")
        lines.append("| Tool | Before | After |")
        lines.append("| --- | --- | --- |")
        for c in tool_changes:
            before = c.old if c.old is not None else "\u2014"
            after = c.new if c.new is not None else "removed"
            if c.describe() == "major update":
                after += " (major)"
            lines.append(f"| {c.name} | {before} | {after} |")
        lines.append("")

    # Debian package changes.
    deb_changes = [c for c in decision.changes if c.kind == "deb"]
    if deb_changes:
        lines.append("## Debian packages")
        lines.append("")
        lines.append(f"{len(deb_changes)} changed:")
        lines.append("")
        for c in deb_changes:
            old_str = c.old if c.old is not None else "\u2014"
            new_str = c.new if c.new is not None else "removed"
            lines.append(f"- `{c.name}` {old_str} \u2192 {new_str}")
        lines.append("")

    # Everything in this image.
    lines.append("## Everything in this image")
    lines.append("")
    lines.append("<details><summary>Tool versions</summary>")
    lines.append("")
    lines.append("| Tool | Version |")
    lines.append("| --- | --- |")
    tools = sorted(
        (name for (kind, name), _ in current.items() if kind == "tool")
    )
    for name in tools:
        lines.append(f"| {name} | {current[('tool', name)]} |")
    lines.append("")
    lines.append("</details>")
    lines.append("")  # blank line before final newline

    return "\n".join(lines)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main(argv: list[str]) -> int:
    """Entry point for the CLI."""
    parser = argparse.ArgumentParser(
        prog="next-version.py",
        description=__doc__.split("\n\n", 2)[2],
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--current", required=True, type=Path)
    parser.add_argument("--previous", type=Path)
    parser.add_argument("--last-tag")
    parser.add_argument("--major", required=True, type=int)
    parser.add_argument("--inputs-changed", action="store_true")
    parser.add_argument("--notes", type=Path)

    args = parser.parse_args(argv)

    try:
        current_text = args.current.read_text(encoding="utf-8")
        current = parse_manifest(current_text, str(args.current))

        previous: Manifest | None = None
        if args.previous is not None:
            previous_text = args.previous.read_text(encoding="utf-8")
            previous = parse_manifest(previous_text, str(args.previous))

        decision = decide(
            current=current,
            major=args.major,
            previous=previous,
            last_tag=args.last_tag,
            inputs_changed=args.inputs_changed,
        )
    except (ReleaseError, OSError) as exc:
        print(f"next-version.py: error: {exc}", file=sys.stderr)
        return 2

    print(f"next-version.py: {decision.reason}", file=sys.stderr)

    if decision.version is not None:
        print(decision.version)
        if args.notes is not None:
            notes = render_notes(decision, current)
            args.notes.write_text(notes, encoding="utf-8")

    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
