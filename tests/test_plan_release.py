"""Run scripts/plan-release.sh against a throwaway git repository and a fake docker.

This tests the shell glue that gathers inputs for scripts/next-version.py,
using a fake docker and a throwaway git repository so the developer's
real environment cannot change the results.
"""
from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parent.parent / "scripts" / "plan-release.sh"

MANIFEST = "tool\tkubectl\t1.36.6\ndeb\tgit\t1:2.47.3-0+deb13u1\n"


# ---------------------------------------------------------------------------
# Fixture
# ---------------------------------------------------------------------------

@pytest.fixture
def env(tmp_path, monkeypatch):
    """Set up a throwaway git repo and a fake docker, then return helpers."""
    # Prevent the developer's git config from changing results.
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", "/dev/null")
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")
    monkeypatch.delenv("GIT_DIR", raising=False)
    monkeypatch.delenv("GIT_WORK_TREE", raising=False)
    monkeypatch.delenv("GIT_INDEX_FILE", raising=False)

    # Create the fake registry: a file per image reference.
    images = tmp_path / "images"
    images.mkdir()

    # Create the fake docker script.
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    fake_docker = bin_dir / "docker"
    fake_docker.write_text(
        "#!/usr/bin/env bash\n"
        'if [[ "$1" != "run" || "$2" != "--rm" || "$3" != "--entrypoint" ||\n'
        '     "$4" != "cat" ]]; then\n'
        '    echo "unsupported docker call" >&2\n'
        '    exit 64\n'
        'fi\n'
        "name=${5//[\\/:@]/_}\n"
        "if [[ -f \"$FAKE_IMAGES/$name\" ]]; then\n"
        '    cat "$FAKE_IMAGES/$name"\n'
        "else\n"
        '    echo "no such image: $5" >&2\n'
        "    exit 1\n"
        "fi\n"
    )
    fake_docker.chmod(0o755)

    # Set PATH so our fake docker comes first.
    monkeypatch.setenv(
        "PATH", str(bin_dir) + ":" + os.environ.get("PATH", "/usr/bin:/bin")
    )

    # Set FAKE_IMAGES so the fake docker can find image files.
    monkeypatch.setenv("FAKE_IMAGES", str(images))

    # The script's own temporary directory goes here, so a test can check it
    # was cleaned up.
    scratch = tmp_path / "scratch"
    scratch.mkdir()
    monkeypatch.setenv("TMPDIR", str(scratch))

    # Create a git repository with initial content.
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-q", "-b", "main"], cwd=repo, check=True)
    (repo / "Dockerfile").write_text("FROM scratch\n")
    (repo / "rootfs" / "usr/local" / "bin").mkdir(parents=True)
    (repo / "rootfs" / "usr/local" / "bin" / "opsbox-entrypoint").write_text(
        "#!/bin/sh\n"
    )
    (repo / "README.md").write_text("readme\n")
    (repo / "MAJOR_VERSION").write_text("1\n")
    subprocess.run(
        ["git", "-c", "user.name=test", "-c", "user.email=test@example.com",
         "-c", "commit.gpgsign=false", "add", "."],
        cwd=repo, check=True
    )
    subprocess.run(
        ["git", "-c", "user.name=test", "-c", "user.email=test@example.com",
         "-c", "commit.gpgsign=false", "commit", "-q", "-m", "initial"],
        cwd=repo, check=True
    )

    class Helpers:
        def __init__(self, repo: Path, images: Path) -> None:
            self.repo = repo
            self.images = images
            self.scratch = repo.parent / "scratch"

        def image(self, ref: str, text: str) -> None:
            """Write a fake image file."""
            name = ref.replace("/", "_").replace(":", "_").replace("@", "_")
            (self.images / name).write_text(text)

        def commit(self, path: str, text: str) -> None:
            """Write and commit a file in the repo."""
            full = self.repo / path
            full.parent.mkdir(parents=True, exist_ok=True)
            full.write_text(text)
            subprocess.run(
                ["git", "-c", "user.name=test", "-c", "user.email=test@example.com",
                 "-c", "commit.gpgsign=false", "add", path],
                cwd=self.repo, check=True
            )
            subprocess.run(
                ["git", "-c", "user.name=test", "-c", "user.email=test@example.com",
                 "-c", "commit.gpgsign=false", "commit", "-q", "-m", "change"],
                cwd=self.repo, check=True
            )

        def tag(self, name: str) -> None:
            """Create a lightweight tag at HEAD."""
            subprocess.run(
                ["git", "-c", "user.name=test", "-c", "user.email=test@example.com",
                 "-c", "commit.gpgsign=false", "tag", name],
                cwd=self.repo, check=True
            )

        def run(self, *args: str) -> subprocess.CompletedProcess[str]:
            """Run plan-release.sh with the given args."""
            return subprocess.run(
                ["bash", str(SCRIPT), *args],
                cwd=self.repo,
                capture_output=True,
                text=True,
                timeout=60,
            )

    return Helpers(repo, images)


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

def test_first_release(env: Helpers) -> None:
    """No tags: the first release gets v1.0.0."""
    env.image("cand", MANIFEST)
    result = env.run("cand", "reg/opsbox")
    assert result.returncode == 0
    assert result.stdout == "v1.0.0\n"


def test_succeeds_and_cleans_up(env: Helpers) -> None:
    """A run that finds a release exits 0 and leaves no temporary files.

    The cleanup trap once named a variable local to main. It ran after main
    returned, found the variable unbound, and turned every successful run
    into exit 1.
    """
    env.image("cand", MANIFEST)
    result = env.run("cand", "reg/opsbox")
    assert result.returncode == 0, result.stderr
    assert "unbound variable" not in result.stderr
    assert list(env.scratch.iterdir()) == []


def test_last_tag_is_the_highest_release(env: Helpers) -> None:
    """Tags v1.2.3, v1.9.9, v1.10.0, v2.0.0-rc1 and nonsense: 1.10.0 wins."""
    # All tags at the same commit.
    for t in ("v1.2.3", "v1.9.9", "v1.10.0", "v2.0.0-rc1", "nonsense"):
        env.tag(t)
    env.image("cand", MANIFEST)
    env.image("reg/opsbox:1.10.0", MANIFEST)
    result = env.run("cand", "reg/opsbox")
    assert result.returncode == 0
    assert result.stdout == ""
    assert "no changes since v1.10.0" in result.stderr


def test_build_file_change_is_a_patch(env: Helpers) -> None:
    """A change to rootfs bumps the patch version."""
    env.tag("v1.0.0")
    env.commit(
        "rootfs/usr/local/bin/opsbox-entrypoint",
        "#!/bin/sh\n# updated\n",
    )
    env.image("cand", MANIFEST)
    env.image("reg/opsbox:1.0.0", MANIFEST)
    result = env.run("cand", "reg/opsbox")
    assert result.returncode == 0
    assert result.stdout == "v1.0.1\n"


def test_refuses_to_release_behind_the_last_tag(env: Helpers) -> None:
    """A run at a commit the last release doesn't descend from fails.

    Re-running an old workflow run after a newer release would otherwise
    publish older code as the next version and move `latest` back onto it.
    """
    env.commit("rootfs/usr/local/bin/opsbox-entrypoint", "#!/bin/sh\n# newer\n")
    env.tag("v1.0.1")
    subprocess.run(["git", "checkout", "-q", "HEAD~1"], cwd=env.repo, check=True)
    env.image("cand", MANIFEST)
    env.image("reg/opsbox:1.0.1", MANIFEST)
    result = env.run("cand", "reg/opsbox")
    assert result.returncode != 0
    assert "not an ancestor" in result.stderr
    assert result.stdout == ""


def test_other_changes_release_nothing(env: Helpers) -> None:
    """A change to README.md alone releases nothing."""
    env.tag("v1.0.0")
    env.commit("README.md", "updated readme\n")
    env.image("cand", MANIFEST)
    env.image("reg/opsbox:1.0.0", MANIFEST)
    result = env.run("cand", "reg/opsbox")
    assert result.returncode == 0
    assert result.stdout == ""


def test_notes_written_when_due(env: Helpers) -> None:
    """--notes writes release notes when a release is due."""
    env.image("cand", MANIFEST)
    notes_file = env.repo.parent / "notes.md"
    result = env.run("--notes", str(notes_file), "cand", "reg/opsbox")
    assert result.returncode == 0
    assert notes_file.exists()
    assert "## Everything in this image" in notes_file.read_text()


def test_missing_previous_image_fails(env: Helpers) -> None:
    """Missing the previous image returns an error."""
    env.tag("v1.0.0")
    env.image("cand", MANIFEST)
    # No "reg/opsbox:1.0.0" image created.
    result = env.run("cand", "reg/opsbox")
    assert result.returncode != 0
    assert "cannot read" in result.stderr


def test_bad_major_version(env: Helpers) -> None:
    """MAJOR_VERSION with a non-numeric value returns an error."""
    (env.repo / "MAJOR_VERSION").write_text("one\n")
    env.image("cand", MANIFEST)
    result = env.run("cand", "reg/opsbox")
    assert result.returncode != 0
    assert "MAJOR_VERSION must be a whole number" in result.stderr


def test_outside_a_repository_fails_clearly(env: Helpers, tmp_path: Path) -> None:
    """Run from a directory that isn't a git repository, it says so.

    The release's publish steps once ran in a temporary directory, where this
    failed with git's own "not a git repository" and stopped every release.
    """
    outside = tmp_path / "outside"
    outside.mkdir()
    env.image("cand", MANIFEST)
    result = subprocess.run(["bash", str(SCRIPT), "cand", "reg/opsbox"], cwd=outside,
                            capture_output=True, text=True, timeout=60)
    assert result.returncode == 1
    assert "run this from inside the opsbox repository" in result.stderr


def test_wrong_arguments(env: Helpers) -> None:
    """Passing only one argument prints usage and exits 2."""
    result = env.run("only-one")
    assert result.returncode == 2
    assert "Usage:" in result.stderr
