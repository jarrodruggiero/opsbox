"""The release workflow's wiring, which no script's own test can see.

A workflow can't run here, so these read its text. They fail when a step or a
flag this depends on is dropped, which nothing else would notice until a
release went wrong.
"""
from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
RELEASE = (ROOT / ".github" / "workflows" / "release.yml").read_text()
CI = (ROOT / ".github" / "workflows" / "ci.yml").read_text()

GUARD = re.compile(r"^\s+if: steps\.version\.outputs\.version != ''$", re.MULTILINE)
OUTPUT = re.compile(r'^\s+echo "version=\$version" >> "\$GITHUB_OUTPUT"$', re.MULTILINE)


def planner_calls(workflow: str) -> list[str]:
    """Each call of plan-release.sh, comments left out."""
    return re.findall(r"^(?!\s*#).*scripts/plan-release\.sh.*$", workflow, re.MULTILINE)


def publish_steps() -> list[str]:
    """The publish job's steps, as text, in order."""
    job = RELEASE.split("\n  publish:\n", 1)[1]
    return re.split(r"\n      - ", job)[1:]


def test_a_release_only_comes_from_the_newest_commit() -> None:
    """The plan and the publish both ask, or a merge during a run breaks it."""
    calls = planner_calls(RELEASE)
    assert len(calls) == 2
    assert all("--only-if-newest" in call for call in calls)


def test_the_pull_request_preview_does_not_ask() -> None:
    """A pull request is never main's newest commit: it would preview nothing."""
    calls = planner_calls(CI)
    assert len(calls) == 1
    assert "--only-if-newest" not in calls[0]


def test_finding_no_release_is_not_a_failure() -> None:
    """A run that main moved past did nothing wrong, so it stays green."""
    version = next(step for step in publish_steps() if step.startswith("name: Version and notes"))
    assert "exit 0" in version
    assert "exit 1" not in version


def test_nothing_is_published_when_the_plan_finds_no_release() -> None:
    """Every step after the version is skipped without one."""
    steps = publish_steps()
    version = next(i for i, step in enumerate(steps) if step.startswith("name: Version and notes"))
    after = steps[version + 1:]
    assert any("publish-image.sh" in step for step in after)
    assert any("attest-build-provenance" in step for step in after)
    assert any("gh release create" in step for step in after)
    for step in after:
        assert GUARD.search(step), step


def test_a_version_reaches_the_steps_that_publish_it() -> None:
    """Lose this and every release is skipped, in a run that stays green."""
    version = next(step for step in publish_steps() if step.startswith("name: Version and notes"))
    assert "id: version" in version
    assert 'if [ -z "$version" ]; then' in version
    assert OUTPUT.search(version)
    assert len(OUTPUT.findall(RELEASE)) == 2  # the plan's and the publish's


def test_runs_wait_for_each_other() -> None:
    """Two runs at once could race the version; cancelling one could orphan a tag."""
    assert "\nconcurrency:\n  group: release\n  cancel-in-progress: false\n" in RELEASE
