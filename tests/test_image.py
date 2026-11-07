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

# The workflows set OPSBOX_REQUIRE_IMAGE beside OPSBOX_IMAGE. Without it, a
# renamed variable would turn every gate that runs these tests into a quiet
# skip, and pytest exits 0 on skips.
if not IMAGE and os.environ.get("OPSBOX_REQUIRE_IMAGE"):
    raise RuntimeError("OPSBOX_REQUIRE_IMAGE is set but OPSBOX_IMAGE is not")

pytestmark = pytest.mark.skipif(not IMAGE, reason="OPSBOX_IMAGE is not set")

# Sticky, world-writable temporary directories, as every Debian has.
SHARED_TMP = {"/tmp", "/var/tmp", "/run/lock", "/var/lock"}


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


def test_github_s_host_keys_are_known() -> None:
    """Git over SSH to GitHub never stops to ask, as nobody is there to answer."""
    result = run("ssh-keygen", "-F", "github.com", "-f", "/etc/ssh/ssh_known_hosts")
    keys = [line for line in result.stdout.splitlines() if line.startswith("github.com ")]
    assert len(keys) == 3


def test_a_python_venv_can_be_made() -> None:
    """Debian's Python needs python3-venv for it, and local-llm's installer makes one."""
    run("python3", "-m", "venv", "/tmp/venv-check")


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
    """--help returns 0 and names the settings and both ways to run."""
    result = run("--help", check=False)
    assert result.returncode == 0
    combined = result.stdout + result.stderr
    for word in ("OPSBOX_WORKDIR", "OPSBOX_MANAGER_PORT", "OPSBOX_MANAGER_HOSTS", "manager", "remote-control"):
        assert word in combined


def test_runs_a_command() -> None:
    """The image can run a simple command."""
    result = run("echo", "hello")
    assert result.stdout == "hello\n"


def image_files(path: str) -> set[str]:
    """The names under path in the image itself, read from a container that
    never starts: a running one can gain files the image doesn't have
    (Docker Desktop's Rosetta writes ~/.cache/rosetta into an emulated amd64
    container)."""
    container = docker("create", IMAGE).stdout.strip()
    try:
        archive = subprocess.run(
            ["docker", "cp", f"{container}:{path}", "-"],
            capture_output=True, timeout=120, check=True,
        ).stdout
    finally:
        docker("rm", container, check=False)
    top = Path(path).name
    with tarfile.open(fileobj=io.BytesIO(archive)) as tar:
        return {Path(m.name).relative_to(top).as_posix()
                for m in tar.getmembers() if m.name != top}


def test_home_has_nothing_baked_in() -> None:
    """The image's home directory holds only useradd's skeleton files.

    Keys and sign-ins belong in the volume.
    """
    assert image_files("/home/opsbox") == {".bashrc", ".bash_logout", ".profile"}


def test_temporary_directories_ship_empty() -> None:
    """Nothing the build's tools wrote to /tmp or /var/tmp ships.

    npm once left a V8 compile cache there, owned by nobody.
    """
    assert image_files("/tmp") == set()
    assert image_files("/var/tmp") == set()


def test_nothing_outside_home_is_writable() -> None:
    """The box's user can change nothing but its home and temp directories.

    COPY --from once kept restic owned by uid 1000, so a session could swap
    it for one that records RESTIC_PASSWORD.
    """
    result = run("sh", "-c", "find / -xdev \\( -path /home -o -path /proc -o -path /sys "
                 "-o -path /dev \\) -prune -o -writable -print 2>/dev/null; true")
    assert set(result.stdout.split()) <= SHARED_TMP


def test_no_setuid_or_setgid_files() -> None:
    """No binary can raise privileges, even without no-new-privileges."""
    result = run("sh", "-c", "find / -xdev -perm /6000 -type f 2>/dev/null; true")
    assert result.stdout == ""


def test_files_are_private_by_default() -> None:
    """What the box's sessions and shells write is for its own user alone."""
    assert run("sh", "-c", "umask").stdout == "0077\n"
    assert run("bash", "-ic", "umask").stdout.splitlines()[-1] == "0077"


def test_mise_trust_is_per_file() -> None:
    """MISE_PARANOID ties trust to one config file's contents."""
    assert run("printenv", "MISE_PARANOID").stdout == "1\n"


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


def waits_with(message: str, *options: str, command: tuple[str, ...] = (), page: bool = False) -> None:
    """Start the image detached and check it keeps running, saying message.

    With page, also fetch the manager page and its files from inside the
    container, the way a browser on the LAN would.
    """
    result = docker("run", "-d", *options, IMAGE, *command)
    container_id = result.stdout.strip()
    try:
        for _ in range(20):
            logs = docker("logs", container_id)
            combined = logs.stdout + logs.stderr
            if message in combined:
                break
            time.sleep(1)
        else:
            pytest.fail(f"timed out waiting for {message!r}")
        state = docker("inspect", "-f", "{{.State.Running}}", container_id)
        assert state.stdout.strip() == "true"
        if page:
            fetched = docker("exec", container_id, "curl", "-fsS", "http://127.0.0.1:8080/")
            assert "Sign in to Claude" in fetched.stdout
            # The vendored terminal made it into the image, under the names
            # the page asks for.
            for name in ("manager.css", "manager.js", "xterm.js", "xterm.css", "xterm-addon-fit.js", "icon.svg"):
                docker("exec", container_id, "curl", "-fsS", "-o", "/dev/null", f"http://127.0.0.1:8080/static/{name}")
    finally:
        docker("rm", "-f", container_id, check=False)


def test_manager_waits_for_sign_in_and_serves_the_page() -> None:
    """The default command serves the page and waits for a sign-in; it does not exit."""
    waits_with("not signed in to Claude yet", page=True)


def test_remote_control_still_waits_for_sign_in() -> None:
    """remote-control runs it without the page, as before the manager."""
    waits_with("not signed in to Claude Code yet", command=("remote-control", "--spawn", "same-dir"))


@pytest.mark.parametrize("command", [(), ("remote-control", "--spawn", "same-dir")], ids=["manager", "remote-control"])
def test_api_key_waits_instead_of_crash_looping(command: tuple[str, ...]) -> None:
    """An API key counts as signed in but can't start Remote Control.

    `claude remote-control` would exit at once, and a restart (by Docker, or
    by the manager) would loop without ever saying why.
    """
    waits_with("can't start Remote Control", "-e", "ANTHROPIC_API_KEY=sk-ant-api03-not-a-real-key", command=command)


def test_manager_refuses_workdir_home() -> None:
    """The manager has the same refusal as remote-control: no trust answer is ever saved for $HOME."""
    result = run("manager", env={"OPSBOX_WORKDIR": "/home/opsbox/"}, check=False)
    assert result.returncode == 1
    assert "trust" in result.stderr


def test_page_settings_reach_a_shell_exactly_as_written() -> None:
    """env.sh exports what the page saved: a token's trailing = survives, and $(...) stays text."""
    result = run(
        "bash",
        "-c",
        r"""mkdir -p ~/.config/opsbox
printf 'TOKEN=abc123==\nCMD=$(echo ran)\n# a comment\nbad-name=x\nSPACES=a b\n' > ~/.config/opsbox/env
. /usr/local/lib/opsbox/env.sh
printf '[%s][%s][%s]' "$TOKEN" "$CMD" "$SPACES"
""",
    )
    assert result.stdout == "[abc123==][$(echo ran)][a b]"


def test_a_setting_named_after_a_readonly_variable_cant_stop_the_box() -> None:
    """The entrypoint sources env.sh under set -e, with readonly variables of its own: UID, for one."""
    result = run(
        "bash",
        "-c",
        r"""set -euo pipefail
readonly WORKDIR=/kept
mkdir -p ~/.config/opsbox
printf 'UID=5\nWORKDIR=/elsewhere\nAFTER=reached\n' > ~/.config/opsbox/env
. /usr/local/lib/opsbox/env.sh
printf '[%s][%s]' "$WORKDIR" "$AFTER"
""",
    )
    assert result.stdout == "[/kept][reached]"


# Run inside the container, because it needs the box's own Python and address.
# A session is issued directly: a real one needs a real claude.ai sign-in.
THROUGH_THE_PAGE = r"""
import base64, http.client, sys, time, urllib.parse
from pathlib import Path
sys.path.insert(0, "/usr/local/lib")
from opsbox.manager.sessions import Sessions
sessions = Sessions(Path.home() / ".config/opsbox/manager")
token = sessions.issue("the image test")
cookie = "opsbox_session=" + token
csrf = sessions.csrf(token)

def post(path, **fields):
    conn = http.client.HTTPConnection("127.0.0.1", 8080, timeout=20)
    conn.request("POST", path, urllib.parse.urlencode({"csrf": csrf, **fields}),
                 {"Cookie": cookie, "Content-Type": "application/x-www-form-urlencoded"})
    status = conn.getresponse().status
    conn.close()
    return status

assert post("/shell/open", cols=90, rows=30) == 204
conn = http.client.HTTPConnection("127.0.0.1", 8080, timeout=20)
conn.request("GET", "/shell/stream", headers={"Cookie": cookie})
stream = conn.getresponse()
assert stream.status == 200
assert post("/shell/input", data="echo shell-$((40+2)) uid-$(id -u) pwd-$PWD term-$TERM size-$(stty size | tr ' ' x) umask-$(umask) path-$PATH; exit\r") == 204
seen = b""
deadline = time.monotonic() + 15
while time.monotonic() < deadline and b"event: end" not in seen:
    seen += stream.fp.readline()
output = b"".join(base64.b64decode(line[6:]) for line in seen.split(b"\n") if line.startswith(b"data: ")).decode()
print(output)

boundary = "----imagetest"
data = bytes(range(256))
body = b"".join([
    (f'--{boundary}\r\nContent-Disposition: form-data; name="csrf"\r\n\r\n{csrf}\r\n').encode(),
    (f'--{boundary}\r\nContent-Disposition: form-data; name="path"\r\n\r\n~/.kube/config\r\n').encode(),
    (f'--{boundary}\r\nContent-Disposition: form-data; name="file"; filename="c"\r\n\r\n').encode(), data,
    (f'\r\n--{boundary}--\r\n').encode(),
])
conn = http.client.HTTPConnection("127.0.0.1", 8080, timeout=20)
conn.request("POST", "/files", body, {"Cookie": cookie, "Content-Type": f"multipart/form-data; boundary={boundary}"})
assert conn.getresponse().status == 303
saved = Path.home() / ".kube/config"
assert saved.read_bytes() == data
print("upload-mode", oct(saved.stat().st_mode & 0o777), oct(saved.parent.stat().st_mode & 0o777))
"""


def test_shell_and_upload_work_locked_down() -> None:
    """Through the page, in the image, with the flags compose uses: bash starts, and a file lands under home."""
    home = "/home/opsbox:uid=1000,gid=1000,mode=0700"
    result = docker(
        "run", "-d", "--read-only", "--tmpfs", "/tmp", "--tmpfs", "/var/tmp", "--tmpfs", home,
        "--cap-drop", "ALL", "--security-opt", "no-new-privileges", IMAGE,
    )
    container_id = result.stdout.strip()
    try:
        for _ in range(20):
            if "serving on port" in docker("logs", container_id).stderr:
                break
            time.sleep(1)
        else:
            pytest.fail("the manager never said it was serving")
        through = docker("exec", container_id, "/usr/bin/python3", "-c", THROUGH_THE_PAGE, check=False)
        assert through.returncode == 0, through.stderr
        assert "shell-42 uid-1000 pwd-/home/opsbox/workspace term-xterm-256color size-30x90 umask-0077" in through.stdout
        # The same PATH `docker exec bash` has: a login shell's /etc/profile would
        # drop the mise shims, and with them a repository's pinned tool versions.
        assert "path-/home/opsbox/.local/bin:/home/opsbox/.local/share/mise/shims:" in through.stdout
        assert "upload-mode 0o600 0o700" in through.stdout
        # A real terminal: job control needs it to be the shell's controlling one.
        assert "no job control" not in through.stdout
        assert "cannot set terminal process group" not in through.stdout
        # What a shell prints stays out of the container's log.
        logs = docker("logs", container_id)
        assert "shell-42" not in logs.stdout + logs.stderr
    finally:
        docker("rm", "-f", container_id, check=False)
