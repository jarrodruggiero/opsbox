"""Tests for keeping ~/.claude/skills a checkout of a skills library, with real git and local repos."""
from __future__ import annotations

import os
import pathlib
import subprocess

import pytest

from opsbox.manager import skills


def git(cwd: pathlib.Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-c", "user.name=t", "-c", "user.email=t@example.com", "-c", "commit.gpgsign=false", *args],
        cwd=cwd, check=True, capture_output=True, text=True,
    ).stdout.strip()


@pytest.fixture
def library(tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch) -> dict:
    """A bare origin with one skill on main, another machine's clone of it, and an empty home."""
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", "/dev/null")
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")
    origin = tmp_path / "origin.git"
    git(tmp_path, "init", "-q", "--bare", "-b", "main", str(origin))
    other = tmp_path / "other"
    git(tmp_path, "clone", "-q", str(origin), str(other))
    (other / "qwen-spec").mkdir()
    (other / "qwen-spec/SKILL.md").write_text("one\n")
    (other / ".gitignore").write_text("synced/\n")
    git(other, "add", "-A")
    git(other, "commit", "-q", "-m", "one")
    git(other, "push", "-q", "origin", "main")
    home = tmp_path / "home"
    home.mkdir()
    return {"url": str(origin), "other": other, "home": home, "dest": home / ".claude/skills",
            "env": {skills.SETTING: str(origin), "PATH": os.environ["PATH"]}}


def push_from_other(lib: dict, text: str) -> str:
    (lib["other"] / "qwen-spec/SKILL.md").write_text(text)
    git(lib["other"], "commit", "-q", "-am", "another machine's change")
    git(lib["other"], "push", "-q", "origin", "main")
    return git(lib["other"], "rev-parse", "HEAD")


def test_with_no_setting_nothing_happens(library: dict) -> None:
    assert skills.sync({"PATH": os.environ["PATH"]}, library["home"]) is None
    assert not library["dest"].exists()


def test_the_first_start_checks_the_library_out(library: dict) -> None:
    note = skills.sync(library["env"], library["home"])
    assert note is not None and note.startswith("skills: checked out")
    assert (library["dest"] / "qwen-spec/SKILL.md").read_text() == "one\n"


def test_claude_ai_s_own_skills_stay_where_they_are(library: dict) -> None:
    """claude.ai syncs into synced/, which may already be there; the library ignores it."""
    (library["dest"] / "synced/docx").mkdir(parents=True)
    (library["dest"] / "synced/docx/SKILL.md").write_text("theirs\n")
    note = skills.sync(library["env"], library["home"])
    assert note is not None and note.startswith("skills: checked out")
    assert (library["dest"] / "synced/docx/SKILL.md").read_text() == "theirs\n"
    assert (library["dest"] / "qwen-spec/SKILL.md").exists()


def test_an_up_to_date_library_says_nothing(library: dict) -> None:
    skills.sync(library["env"], library["home"])
    assert skills.sync(library["env"], library["home"]) is None


def test_another_machine_s_change_arrives_at_the_next_start(library: dict) -> None:
    skills.sync(library["env"], library["home"])
    new = push_from_other(library, "two\n")
    assert skills.sync(library["env"], library["home"]) == f"skills: updated to {new[:7]}"
    assert (library["dest"] / "qwen-spec/SKILL.md").read_text() == "two\n"


@pytest.mark.parametrize("how", ["edited", "branch", "commit"])
def test_work_on_this_box_is_never_overwritten(library: dict, how: str) -> None:
    """Changed, on a proposal's branch, or committed but not pushed: all someone's work."""
    skills.sync(library["env"], library["home"])
    dest = library["dest"]
    if how == "edited":
        # Somewhere the next change doesn't touch, so only the check for changes can stop it.
        (dest / "mine").mkdir()
        (dest / "mine/SKILL.md").write_text("mine\n")
    elif how == "branch":
        git(dest, "switch", "-q", "-c", "opsbox/qwen-spec")
    else:
        (dest / "qwen-spec/SKILL.md").write_text("mine\n")
        git(dest, "commit", "-q", "-am", "mine")
    push_from_other(library, "two\n")
    note = skills.sync(library["env"], library["home"])
    if how == "commit":
        assert note == "skills: not updated, because ~/.claude/skills has commits that main doesn't"
        assert (dest / "qwen-spec/SKILL.md").read_text() == "mine\n"
    else:
        assert note == "skills: not updated, because ~/.claude/skills has changes or is on another branch"
        assert (dest / "qwen-spec/SKILL.md").read_text() == "one\n"
    if how == "edited":
        assert (dest / "mine/SKILL.md").read_text() == "mine\n"


def test_a_checkout_of_something_else_is_left_alone(library: dict, tmp_path: pathlib.Path) -> None:
    skills.sync(library["env"], library["home"])
    elsewhere = tmp_path / "elsewhere.git"
    git(tmp_path, "init", "-q", "--bare", "-b", "main", str(elsewhere))
    git(library["dest"], "remote", "set-url", "origin", str(elsewhere))
    note = skills.sync(library["env"], library["home"])
    assert note is not None and "left alone" in note
    assert git(library["dest"], "remote", "get-url", "origin") == str(elsewhere)


def test_an_unreachable_library_is_said_and_tried_again_next_time(library: dict) -> None:
    env = {**library["env"], skills.SETTING: str(library["home"] / "missing.git")}
    note = skills.sync(env, library["home"])
    assert note is not None and note.startswith("skills: couldn't")
    # Nothing half-made is left to block the next try.
    assert not (library["dest"] / ".git").exists()
    assert skills.sync(library["env"], library["home"]).startswith("skills: checked out")


def test_skills_put_there_by_hand_are_never_overwritten(library: dict) -> None:
    """The first checkout refuses rather than replace a file the library also has."""
    (library["dest"] / "qwen-spec").mkdir(parents=True)
    (library["dest"] / "qwen-spec/SKILL.md").write_text("by hand\n")
    note = skills.sync(library["env"], library["home"])
    assert note is not None and note.startswith("skills: couldn't")
    assert (library["dest"] / "qwen-spec/SKILL.md").read_text() == "by hand\n"
    assert not (library["dest"] / ".git").exists()


def test_git_never_waits_for_a_password_or_a_host_key(library: dict, monkeypatch: pytest.MonkeyPatch) -> None:
    """Nobody is there to answer, and Remote Control would wait behind it."""
    seen = {}
    real = subprocess.run

    def spy(args, **kwargs):
        seen.update(kwargs["env"])
        return real(args, **kwargs)

    monkeypatch.setattr(skills.subprocess, "run", spy)
    skills.sync(library["env"], library["home"])
    assert seen["GIT_TERMINAL_PROMPT"] == "0"
    assert "BatchMode=yes" in seen["GIT_SSH_COMMAND"]


def test_a_timeout_in_the_first_checkout_leaves_nothing_to_block_the_next(library: dict, monkeypatch: pytest.MonkeyPatch) -> None:
    real = subprocess.run

    def slow_fetch(args, **kwargs):
        if "fetch" in args:
            raise subprocess.TimeoutExpired(args, skills.TIMEOUT)
        return real(args, **kwargs)

    monkeypatch.setattr(skills.subprocess, "run", slow_fetch)
    note = skills.sync(library["env"], library["home"])
    assert note is not None and "took too long" in note
    assert not (library["dest"] / ".git").exists()
    monkeypatch.setattr(skills.subprocess, "run", real)
    assert skills.sync(library["env"], library["home"]).startswith("skills: checked out")


def test_a_checkout_left_half_made_is_started_again(library: dict) -> None:
    """Say the manager stopped mid-way: a repository with no commit is no one's work."""
    library["dest"].mkdir(parents=True)
    git(library["dest"], "init", "-q", "-b", "main")
    git(library["dest"], "remote", "add", "origin", library["url"])
    assert skills.sync(library["env"], library["home"]).startswith("skills: checked out")
    assert (library["dest"] / "qwen-spec/SKILL.md").exists()


@pytest.mark.parametrize("when", ["first", "update"])
def test_a_library_with_files_in_synced_is_refused(library: dict, when: str) -> None:
    """Git treats ignored files as expendable, so it would overwrite claude.ai's, then delete them."""
    if when == "update":
        skills.sync(library["env"], library["home"])
    (library["dest"] / "synced/docx").mkdir(parents=True)
    (library["dest"] / "synced/docx/SKILL.md").write_text("theirs\n")
    other = library["other"]
    (other / "synced/docx").mkdir(parents=True)
    (other / "synced/docx/SKILL.md").write_text("upstream\n")
    git(other, "add", "-f", "synced/docx/SKILL.md")
    git(other, "commit", "-q", "-m", "a file where claude.ai keeps its own")
    git(other, "push", "-q", "origin", "main")
    note = skills.sync(library["env"], library["home"])
    assert note is not None and "synced/" in note
    assert (library["dest"] / "synced/docx/SKILL.md").read_text() == "theirs\n"


def test_claude_ai_s_skills_dont_count_as_changes_even_unignored(library: dict) -> None:
    """A library that doesn't ignore synced/ still updates."""
    other = library["other"]
    git(other, "rm", "-q", ".gitignore")
    git(other, "commit", "-q", "-m", "no ignore file")
    git(other, "push", "-q", "origin", "main")
    skills.sync(library["env"], library["home"])
    (library["dest"] / "synced/docx").mkdir(parents=True)
    (library["dest"] / "synced/docx/SKILL.md").write_text("theirs\n")
    new = push_from_other(library, "two\n")
    assert skills.sync(library["env"], library["home"]) == f"skills: updated to {new[:7]}"


def test_a_token_in_the_url_never_reaches_the_console(library: dict) -> None:
    """The page hides a setting with one, and the console is on the page."""
    env = {**library["env"], skills.SETTING: "https://x-access-token:ghp_secret123@127.0.0.1:9/x.git"}
    note = skills.sync(env, library["home"])
    assert note is not None and "ghp_secret123" not in note
    assert "***@127.0.0.1" in note


def test_a_setting_that_looks_like_an_option_is_refused(library: dict) -> None:
    env = {**library["env"], skills.SETTING: "--upload-pack=touch /tmp/x"}
    assert skills.sync(env, library["home"]) == f"skills: {skills.SETTING} must be a repository's URL"
