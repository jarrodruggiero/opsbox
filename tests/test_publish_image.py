"""Run scripts/publish-image.sh against a fake docker.

The fake answers `docker buildx imagetools inspect` from files, one per
image reference, and records what `docker buildx imagetools create` would
have tagged, so these tests need no registry.
"""
from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parent.parent / "scripts" / "publish-image.sh"
REPO = "ghcr.io/owner/opsbox"
AMD64 = f"{REPO}@sha256:{'a' * 64}"
ARM64 = f"{REPO}@sha256:{'b' * 64}"

FAKE_DOCKER = r"""#!/usr/bin/env bash
set -euo pipefail
[[ "$1 $2" == "buildx imagetools" ]] || exit 64
case "$3" in
  inspect)
    name=${4//[\/:@]/_}
    if [[ -f "$FAKE_IMAGES/$name.error" ]]; then
      cat "$FAKE_IMAGES/$name.error" >&2; exit 1
    elif [[ -f "$FAKE_IMAGES/$name" ]]; then
      cat "$FAKE_IMAGES/$name"
    else
      echo "ERROR: $4: not found" >&2; exit 1
    fi
    ;;
  create)
    shift 3
    printf '%s\n' "$@" > "$FAKE_LOG"
    ;;
  *) exit 64 ;;
esac
"""


@pytest.fixture
def fake(tmp_path, monkeypatch):
    """A fake docker on PATH; returns (images dir, create log path)."""
    images = tmp_path / "images"
    images.mkdir()
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    docker = bin_dir / "docker"
    docker.write_text(FAKE_DOCKER)
    docker.chmod(0o755)
    log = tmp_path / "create.log"
    monkeypatch.setenv("PATH", f"{bin_dir}:{Path('/usr/bin')}:{Path('/bin')}")
    monkeypatch.setenv("FAKE_IMAGES", str(images))
    monkeypatch.setenv("FAKE_LOG", str(log))
    return images, log


def image(images: Path, ref: str, text: str) -> None:
    """Make the fake registry answer for ref."""
    (images / ref.replace("/", "_").replace(":", "_").replace("@", "_")).write_text(text)


def run(*args: str) -> subprocess.CompletedProcess[str]:
    """Run publish-image.sh."""
    return subprocess.run(["bash", str(SCRIPT), *args], capture_output=True, text=True, timeout=60)


def test_new_version_gets_all_four_tags(fake) -> None:
    """vX.Y.Z publishes X.Y.Z, X.Y, X and latest from both sources."""
    _, log = fake
    result = run(REPO, "v1.2.3", AMD64, ARM64)
    assert result.returncode == 0, result.stderr
    assert log.read_text().splitlines() == [
        "--tag", f"{REPO}:1.2.3", "--tag", f"{REPO}:1.2", "--tag", f"{REPO}:1",
        "--tag", f"{REPO}:latest", AMD64, ARM64,
    ]


def test_published_version_is_refused(fake) -> None:
    """A published X.Y.Z is never tagged again, whatever it holds.

    plan-release.sh has already moved past any version with an image, so
    reaching one here means something is wrong: stop rather than move a tag
    people pinned.
    """
    images, log = fake
    image(images, f"{REPO}:1.2.3", "{}")
    result = run(REPO, "v1.2.3", AMD64, ARM64)
    assert result.returncode == 1
    assert "already published" in result.stderr
    assert not log.exists()


def test_registry_error_is_not_read_as_unpublished(fake) -> None:
    """Only "not found" about this image means it's free; any other error stops."""
    images, log = fake
    image(images, f"{REPO}:1.2.3.error", "ERROR: unexpected status 500\n")
    result = run(REPO, "v1.2.3", AMD64, ARM64)
    assert result.returncode == 1
    assert "cannot inspect" in result.stderr
    assert not log.exists()


def test_another_not_found_is_not_read_as_unpublished(fake) -> None:
    """A missing credential helper says "not found" about an image that exists."""
    images, log = fake
    image(images, f"{REPO}:1.2.3.error", "ERROR: error getting credentials - err: exec: \"docker-credential-x\": executable file not found in $PATH, out: ``\n")
    result = run(REPO, "v1.2.3", AMD64, ARM64)
    assert result.returncode == 1
    assert "cannot inspect" in result.stderr
    assert not log.exists()


def test_bad_version(fake) -> None:
    """Only vX.Y.Z versions are published."""
    _, log = fake
    result = run(REPO, "1.2.3", AMD64, ARM64)
    assert result.returncode == 1
    assert "not a vX.Y.Z version" in result.stderr
    assert not log.exists()


def test_wrong_arguments() -> None:
    """Fewer than three arguments prints usage and exits 2."""
    result = run(REPO, "v1.2.3")
    assert result.returncode == 2
    assert "Usage:" in result.stderr
