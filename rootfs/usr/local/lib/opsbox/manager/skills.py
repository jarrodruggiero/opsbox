"""Keeps ~/.claude/skills a checkout of a skills library, when OPSBOX_SKILLS_REPO names one.

Brought up to date each time Remote Control starts, so a Restart picks up a changed
skill. It only ever fast-forwards: a checkout with changes, on another branch, or
with commits its origin doesn't have, is left as it is, and the console says so,
because that's someone's work. Git uses the box's own credentials, such as its SSH
key from the manager page.

claude.ai syncs its own skills into synced/, which git would treat as expendable
if the library ever tracked a path there, so a library that does is refused.
"""
from __future__ import annotations

import re
import shutil
import subprocess
from pathlib import Path

SETTING = "OPSBOX_SKILLS_REPO"

# Each git call. Held this long, Remote Control's start waits, so SSH gives up on
# an unreachable host well before it.
TIMEOUT = 60
SSH = "ssh -o BatchMode=yes -o ConnectTimeout=10 -o ServerAliveInterval=5 -o ServerAliveCountMax=2"

# claude.ai's own skills, beside the library's.
SYNCED = "synced"

# Settings that point git at another repository: in the env file by accident, they
# would make the first checkout re-initialise something else.
REPOSITORY_VARIABLES = ("GIT_DIR", "GIT_WORK_TREE", "GIT_INDEX_FILE", "GIT_OBJECT_DIRECTORY",
                        "GIT_ALTERNATE_OBJECT_DIRECTORIES", "GIT_COMMON_DIR", "GIT_NAMESPACE")


def shown(url: str) -> str:
    """The URL as the console shows it: a token in it would undo the page's hiding."""
    return re.sub(r"//[^/@]*@", "//***@", url)


def sync(env: dict[str, str], home: Path) -> str | None:
    """Bring the library up to date; a line for the console when there's something to say."""
    url = env.get(SETTING, "").strip()
    if not url:
        return None
    if url.startswith("-"):
        return f"skills: {SETTING} must be a repository's URL"
    dest = home / ".claude/skills"
    git_env = {name: value for name, value in env.items() if name not in REPOSITORY_VARIABLES}
    # Nobody is there to type a password or accept a host key, and Remote Control
    # would wait behind the prompt. GitHub's host keys are pinned in the image.
    git_env["GIT_TERMINAL_PROMPT"] = "0"

    def git(*args: str) -> subprocess.CompletedProcess[str]:
        # No hooks, whatever the box's global config says: the tree is the library's.
        return subprocess.run(["git", "-c", "core.hooksPath=/dev/null", "-C", str(dest), *args],
                              env=git_env, stdin=subprocess.DEVNULL, capture_output=True,
                              text=True, timeout=TIMEOUT)

    def why(done: subprocess.CompletedProcess[str]) -> str:
        lines = (done.stderr or done.stdout).strip().splitlines()
        return shown(lines[-1]) if lines else f"git exited {done.returncode}"

    def touches_synced() -> bool:
        return bool(git("ls-tree", "--name-only", "origin/main", "--", SYNCED).stdout.strip())

    try:
        dest.mkdir(parents=True, exist_ok=True)
        # The box's own SSH setting wins, if it has one; the env would override it.
        if "GIT_SSH_COMMAND" not in git_env and not git("config", "--global", "core.sshCommand").stdout.strip():
            git_env["GIT_SSH_COMMAND"] = SSH

        # A checkout with no commit yet is one a first try left half-made.
        if (dest / ".git").is_dir() and git("rev-parse", "--verify", "-q", "HEAD").returncode != 0:
            shutil.rmtree(dest / ".git", ignore_errors=True)

        if not (dest / ".git").exists():
            return _first_checkout(dest, url, git, why, touches_synced)

        origin = git("remote", "get-url", "origin").stdout.strip()
        if origin != url:
            return f"skills: ~/.claude/skills comes from {shown(origin) or 'nowhere'}, not {shown(url)}, so it's left alone"
        done = git("fetch", "-q", "origin")
        if done.returncode != 0:
            return f"skills: couldn't reach {shown(url)}, so they may be out of date: {why(done)}"
        branch = git("rev-parse", "--abbrev-ref", "HEAD").stdout.strip()
        changes = git("status", "--porcelain", "--", ".", f":!{SYNCED}").stdout.strip()
        if branch != "main" or changes:
            return "skills: not updated, because ~/.claude/skills has changes or is on another branch"
        if touches_synced():
            return f"skills: not updated, because the library has files in {SYNCED}/, where claude.ai keeps its own"
        before = git("rev-parse", "HEAD").stdout.strip()
        done = git("merge", "-q", "--ff-only", "origin/main")
        if done.returncode != 0:
            return "skills: not updated, because ~/.claude/skills has commits that main doesn't"
        after = git("rev-parse", "HEAD").stdout.strip()
        return None if after == before else f"skills: updated to {after[:7]}"
    except subprocess.TimeoutExpired:
        return f"skills: {shown(url)} took too long, so they may be out of date"
    except OSError as failed:
        return f"skills: couldn't update: {failed.strerror or failed}"


def _first_checkout(dest: Path, url: str, git, why, touches_synced) -> str:
    """In place rather than a clone, because claude.ai's synced/ may be there already."""
    finished = False
    try:
        for step in (("init", "-q", "-b", "main"), ("remote", "add", "origin", url), ("fetch", "-q", "origin")):
            done = git(*step)
            if done.returncode != 0:
                return f"skills: couldn't check out {shown(url)}: {why(done)}"
        if touches_synced():
            return "skills: not checked out, because the library has files in synced/, where claude.ai keeps its own"
        # Refuses rather than replace a file that's in the way.
        done = git("checkout", "-q", "-b", "main", "--track", "origin/main")
        if done.returncode != 0:
            return f"skills: couldn't check out {shown(url)}: {why(done)}"
        finished = True
        return f"skills: checked out {shown(url)} at {git('rev-parse', '--short', 'HEAD').stdout.strip()}"
    finally:
        if not finished:
            # Whatever stopped it, a timeout included: nothing half-made left to block the next try.
            shutil.rmtree(dest / ".git", ignore_errors=True)
