"""Run scripts/publish-image.sh against a fake docker.

The fake answers `docker buildx imagetools inspect` from files, one per
image reference, and records what `docker buildx imagetools create` would
have tagged, so these tests need no registry.
"""
from __future__ import annotations

import json
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


def index(*platform_digests: str) -> str:
    """An image index as `imagetools inspect --format '{{json .Manifest}}'`
    prints it, with an attestation manifest beside each image."""
    manifests = []
    for arch, digest in zip(("amd64", "arm64"), platform_digests):
        manifests.append({"digest": digest, "platform": {"os": "linux", "architecture": arch}})
        manifests.append({"digest": "sha256:" + "f" * 64, "platform": {"os": "unknown", "architecture": "unknown"}})
    return json.dumps({"mediaType": "application/vnd.oci.image.index.v1+json",
                       "digest": "sha256:" + "e" * 64, "manifests": manifests})


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
    monkeypatch.setenv("PATH", f"{bin_dir}:{Path('/usr/bin')}:{Path('/bin')}:/opt/homebrew/bin:/usr/local/bin")
    monkeypatch.setenv("FAKE_IMAGES", str(images))
    monkeypatch.setenv("FAKE_LOG", str(log))
    # Each per-architecture source is an index of one image and its
    # attestation, as build-push-action pushes it.
    image(images, AMD64, index("sha256:" + "1" * 64))
    image(images, ARM64, json.dumps({"mediaType": "application/vnd.oci.image.index.v1+json",
                                     "manifests": [{"digest": "sha256:" + "2" * 64,
                                                    "platform": {"os": "linux", "architecture": "arm64"}}]}))
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


def test_same_images_are_tagged_again(fake) -> None:
    """A re-run after tagging succeeded but the release failed is safe."""
    images, log = fake
    image(images, f"{REPO}:1.2.3", index("sha256:" + "1" * 64, "sha256:" + "2" * 64))
    result = run(REPO, "v1.2.3", AMD64, ARM64)
    assert result.returncode == 0, result.stderr
    assert log.exists()


def test_different_images_are_refused(fake) -> None:
    """A published X.Y.Z is never replaced by a different build."""
    images, log = fake
    image(images, f"{REPO}:1.2.3", index("sha256:" + "1" * 64, "sha256:" + "9" * 64))
    result = run(REPO, "v1.2.3", AMD64, ARM64)
    assert result.returncode == 1
    assert "already published with different images" in result.stderr
    assert not log.exists()


def test_registry_error_is_not_read_as_unpublished(fake) -> None:
    """Only "not found" means the version is free; any other error stops."""
    images, log = fake
    image(images, f"{REPO}:1.2.3.error", "ERROR: unexpected status 500\n")
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
