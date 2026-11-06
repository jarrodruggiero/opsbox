"""Configure sys.path and provide a fake_claude fixture for tests."""
from __future__ import annotations

import pathlib
import sys
import textwrap

import pytest

# Put rootfs/usr/local/lib on sys.path so `from opsbox.manager import ptyproc, claude` works.
_repo_root = pathlib.Path(__file__).resolve().parents[2]
_lib_path = _repo_root / "rootfs" / "usr" / "local" / "lib"
sys.path.insert(0, str(_lib_path))


@pytest.fixture
def fake_claude(tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch) -> pathlib.Path:
    """Write a wrapper script that runs fake_claude.py, patch claude.CLAUDE to use it."""
    # Determine the path to the real fake_claude.py (same directory as this file).
    fake_script = pathlib.Path(__file__).resolve().parent / "fake_claude.py"

    wrapper = tmp_path / "claude"
    wrapper.write_text(
        textwrap.dedent(
            f"""\
            #!{sys.executable}
            import sys
            import runpy
            runpy.run_path(r'{fake_script}', run_name='__main__')
            """
        )
    )
    wrapper.chmod(0o755)

    # Set up FAKE_CLAUDE_HOME for the fake to use as its state directory.
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("FAKE_CLAUDE_HOME", str(home))

    # Patch claude.CLAUDE to point at our wrapper.
    import opsbox.manager.claude

    monkeypatch.setattr(opsbox.manager.claude, "CLAUDE", str(wrapper))
    return wrapper
