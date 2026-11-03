"""Run the built image and check how it behaves.

These tests run the image named by OPSBOX_IMAGE (CI sets it to the image
it just built) and are skipped without it, so a plain `pytest tests` on
a laptop or in the test job runs only the other tests.
"""
from __future__ import annotations

import importlib
import importlib.machinery
import importlib.util
import io
import os
import subprocess
import tarfile
import time
from pathlib import Path

import pytest

IMAGE = os.environ.get("OPSBOX_IMAGE")
pytestmark = pytest.mark.skipif(not IMAGE, reason="OPSBOX_IMAGE is not set")


def tools() -> set[str]:
    """Load the TOOLS table from rootfs/usr/local/bin/opsbox-versions."""
    path = Path(__file__).resolve().parent.parent / "rootfs" / "usr" / "local" / "bin" / "opsbox-versions"
    spec = importlib.util.spec_from_loader(
        "opsbox_versions", loader=importlib.machinery.SourceFileLoader("opsbox_versions", str(path))
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return set(module.TOOLS)


def docker(*args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
    """Run a docker command with standard options."""
    return subprocess.run(
        ["docker", *args],
        capture_output=True,
        text=True,
        timeout=120,
        check=check,
    )


def run(
    *args: str,
    env: dict[str, str] | None = None,
    user: str | None = None,
    check: bool = True,
) -> subprocess.CompletedProcess[str]:
    """Run a command inside the image."""
    # Docker's own options go before the image; anything after it is the
    # container's command.
    cmd = ["docker", "run", "--rm"]
    if env:
        for key, value in env.items():
            cmd.extend(["-e", f"{key}={value}"])
    if user is not None:
        cmd.extend(["--user", user])
    cmd.extend([IMAGE, *args])
    return subprocess.run(
        cmd,
        capture_output=True,
        text=True,
        timeout=120,
        check=check,
    )


def test_runs_as_opsbox() -> None:
    """The image runs as the opsbox user (uid 1000)."""
    result = run("id", "-u")
    assert result.stdout == "1000\n"
    result = run("id", "-un")
    assert result.stdout == "opsbox\n"


def test_no_sudo() -> None:
    """The image has no sudo binary."""
    result = run("sh", "-c", "command -v sudo", check=False)
    assert result.returncode != 0


def test_tini_is_pid_1() -> None:
    """The init process is tini (PID 1)."""
    result = run("cat", "/proc/1/comm")
    assert result.stdout == "tini\n"


def test_versions_file_lists_every_tool() -> None:
    """The versions file lists every tool in the TOOLS set."""
    result = run("cat", "/usr/local/share/opsbox/versions.txt")
    lines = result.stdout.strip().splitlines()
    tool_names = {line.split("\t")[1] for line in lines if line.startswith("tool\t")}
    assert tool_names == tools()
    deb_names = {line.split("\t")[1] for line in lines if line.startswith("deb\t")}
    assert "git" in deb_names
    assert "openssh-client" in deb_names


def test_help() -> None:
    """--help returns 0 and mentions OPSBOX_WORKDIR."""
    result = run("--help", check=False)
    assert result.returncode == 0
    combined = result.stdout + result.stderr
    assert "OPSBOX_WORKDIR" in combined


def test_runs_a_command() -> None:
    """The image can run a simple command."""
    result = run("echo", "hello")
    assert result.stdout == "hello\n"


def test_home_has_nothing_baked_in() -> None:
    """The image's home directory holds only useradd's skeleton files.

    Keys and sign-ins belong in the volume. This reads the image's own files
    from a container that never starts, because a running one can gain files
    the image doesn't have: Docker Desktop's Rosetta writes ~/.cache/rosetta
    into an emulated amd64 container.
    """
    container = docker("create", IMAGE).stdout.strip()
    try:
        archive = subprocess.run(
            ["docker", "cp", f"{container}:/home/opsbox", "-"],
            capture_output=True, timeout=120, check=True,
        ).stdout
    finally:
        docker("rm", container, check=False)
    with tarfile.open(fileobj=io.BytesIO(archive)) as tar:
        names = {Path(m.name).relative_to("opsbox").as_posix()
                 for m in tar.getmembers() if m.name != "opsbox"}
    assert names == {".bashrc", ".bash_logout", ".profile"}


def test_checkpoint_disabled() -> None:
    """CHECKPOINT_DISABLE is set to 1."""
    result = run("printenv", "CHECKPOINT_DISABLE")
    assert result.stdout == "1\n"


def test_unwritable_home_is_refused() -> None:
    """Running as a different user with HOME at /home/opsbox fails."""
    result = run(
        "remote-control",
        env={"HOME": "/home/opsbox"},
        user="4321:4321",
        check=False,
    )
    assert result.returncode == 1
    stderr = result.stderr
    assert "4321" in stderr
    assert "chown" in stderr


def test_workdir_cannot_be_home() -> None:
    """Setting OPSBOX_WORKDIR to /home/opsbox/ fails with a trust warning."""
    result = run(
        "remote-control",
        env={"OPSBOX_WORKDIR": "/home/opsbox/"},
        check=False,
    )
    assert result.returncode == 1
    stderr = result.stderr
    assert "trust" in stderr


def test_waits_for_sign_in() -> None:
    """The image waits for sign-in; it does not exit immediately."""
    result = docker("run", "-d", IMAGE)
    container_id = result.stdout.strip()
    try:
        for _ in range(20):
            logs = docker("logs", container_id)
            combined = logs.stdout + logs.stderr
            if "not signed in to Claude Code yet" in combined:
                break
            time.sleep(1)
        else:
            pytest.fail("timed out waiting for sign-in message")
        state = docker("inspect", "-f", "{{.State.Running}}", container_id)
        assert state.stdout.strip() == "true"
    finally:
        docker("rm", "-f", container_id, check=False)
