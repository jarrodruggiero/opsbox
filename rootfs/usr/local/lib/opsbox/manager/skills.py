"""Keeps ~/.claude/skills a checkout of a skills library, when OPSBOX_SKILLS_REPO names one.

Brought up to date each time Remote Control starts, so a Restart picks up a changed
skill. It only ever fast-forwards: a checkout with changes, on another branch, or
with commits its origin doesn't have, is left as it is, and the console says so,
because that's someone's work. Git uses the box's own credentials, such as its SSH
key from the manager page.
"""
from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

SETTING = "OPSBOX_SKILLS_REPO"

# A fetch over a slow link, but not long enough to keep Remote Control from starting.
TIMEOUT = 60


def sync(env: dict[str, str], home: Path) -> str | None:
    """Bring the library up to date; a line for the console when there's something to say."""
    url = env.get(SETTING, "").strip()
    if not url:
        return None
    dest = home / ".claude/skills"
    # Nobody is there to type a password or accept a host key, and Remote Control
    # would wait behind the prompt. GitHub's host keys are pinned in the image.
    git_env = {**env, "GIT_TERMINAL_PROMPT": "0",
               "GIT_SSH_COMMAND": env.get("GIT_SSH_COMMAND", "ssh -o BatchMode=yes")}

    def git(*args: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(["git", "-C", str(dest), *args], env=git_env,
                              capture_output=True, text=True, timeout=TIMEOUT)

    def why(done: subprocess.CompletedProcess[str]) -> str:
        lines = (done.stderr or done.stdout).strip().splitlines()
        return lines[-1] if lines else f"git exited {done.returncode}"

    try:
        if not (dest / ".git").exists():
            # In place rather than a clone, because claude.ai's synced/ may be there
            # already. The checkout refuses to replace a file that's in the way.
            dest.mkdir(parents=True, exist_ok=True)
            for step in (("init", "-q", "-b", "main"), ("remote", "add", "origin", url),
                         ("fetch", "-q", "origin"), ("checkout", "-q", "-b", "main", "--track", "origin/main")):
                done = git(*step)
                if done.returncode != 0:
                    # Nothing half-made left behind to block the next try.
                    shutil.rmtree(dest / ".git", ignore_errors=True)
                    return f"skills: couldn't check out {url}: {why(done)}"
            return f"skills: checked out {url} at {git('rev-parse', '--short', 'HEAD').stdout.strip()}"

        origin = git("remote", "get-url", "origin").stdout.strip()
        if origin != url:
            return f"skills: ~/.claude/skills comes from {origin or 'nowhere'}, not {url}, so it's left alone"
        done = git("fetch", "-q", "origin")
        if done.returncode != 0:
            return f"skills: couldn't reach {url}, so they may be out of date: {why(done)}"
        branch = git("rev-parse", "--abbrev-ref", "HEAD").stdout.strip()
        if branch != "main" or git("status", "--porcelain").stdout.strip():
            return "skills: not updated, because ~/.claude/skills has changes or is on another branch"
        before = git("rev-parse", "HEAD").stdout.strip()
        done = git("merge", "-q", "--ff-only", "origin/main")
        if done.returncode != 0:
            return "skills: not updated, because ~/.claude/skills has commits that main doesn't"
        after = git("rev-parse", "HEAD").stdout.strip()
        return None if after == before else f"skills: updated to {after[:7]}"
    except (OSError, subprocess.TimeoutExpired) as failed:
        return f"skills: couldn't update: {failed}"
