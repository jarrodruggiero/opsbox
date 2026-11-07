"""A shell on the box, in the page's console.

Each signed-in browser gets one, as the box's own user, which survives a page
reload and a short disconnect. A session can already run anything by uploading a
file to the right path, so this adds no new power over the box: it's the same
trust, made usable. Its output goes only to the browser that opened it, never to
the container's log, because what people type and print in a shell can be secret.

A shell that nobody has viewed for 12 hours is closed, unless something is running
in it: a long terraform apply must survive the tab being on something else.
Signing a browser out closes its shell at once.
"""
from __future__ import annotations

import os
import queue
import threading
import time
from pathlib import Path

from .keys import Refused, read_env_file
from .ptyproc import PtyProcess
from .supervisor import Console

# Not a login shell: Debian's /etc/profile resets PATH, which would drop the mise
# shims and ~/.local/bin that `docker exec bash` has. Its terminal makes it
# interactive, so it still reads bash.bashrc, with the umask and the settings.
BASH = ["/bin/bash"]


class Shell:
    """One shell process, and the console that keeps what it printed."""

    def __init__(self, argv: list[str], *, env: dict[str, str], cwd: str, cols: int, rows: int) -> None:
        # Not collapsed: a shell can run anything, and what looks like a repaint
        # to Remote Control's console might not be one here.
        self.console = Console(collapse_repaints=False)
        self._proc = PtyProcess(argv, env=env, cwd=cwd, cols=cols, rows=rows)
        self._viewers = 0
        self._last_viewed = time.monotonic()
        self._lock = threading.Lock()

    def start(self) -> None:
        """Start the process, and copy its output to the console until it ends."""
        output = self._proc.subscribe()
        self._proc.start()

        def pump() -> None:
            while True:
                try:
                    chunk = output.get(timeout=1.0)
                except queue.Empty:
                    # On Linux a job left behind keeps the terminal open after the
                    # shell has gone, and the page should still say it has ended.
                    if self._proc.running():
                        continue
                    break
                if chunk is None:
                    break
                self.console.feed(chunk)
            self.console.close()

        threading.Thread(target=pump, daemon=True).start()

    def running(self) -> bool:
        """Whether the shell is still there."""
        return self._proc.running()

    def write(self, data: bytes) -> bool:
        """Type into the shell; False if it has gone or isn't reading."""
        try:
            self._proc.write(data)
        except OSError:
            return False
        return True

    def resize(self, cols: int, rows: int) -> None:
        """Match the size of the browser's terminal."""
        self._proc.resize(cols, rows)

    def close(self) -> None:
        """End the shell, the way closing a terminal does."""
        self._proc.hang_up()

    def busy(self) -> bool:
        """Whether a command is running in it, rather than it waiting at its prompt."""
        return self._proc.busy()

    def viewer_joined(self) -> None:
        """A browser started streaming this shell."""
        with self._lock:
            self._viewers += 1

    def viewer_left(self) -> None:
        """A browser stopped streaming it, which starts the idle clock."""
        with self._lock:
            self._viewers -= 1
            self._last_viewed = time.monotonic()

    def idle_for(self) -> float:
        """Seconds since anyone was viewing it; 0 while someone is."""
        with self._lock:
            return 0.0 if self._viewers else time.monotonic() - self._last_viewed


class Shells:
    """The open shells, one per browser session."""

    def __init__(
        self,
        *,
        workdir: str,
        env_file: Path,
        argv: list[str] | None = None,
        limit: int = 8,
        idle_seconds: float = 43200,
    ) -> None:
        self.workdir = workdir
        self.env_file = env_file
        self.argv = argv or BASH
        self.limit = limit
        self.idle_seconds = idle_seconds
        self._shells: dict[str, Shell] = {}
        self._lock = threading.Lock()
        threading.Thread(target=self._reap_loop, daemon=True).start()

    def open(self, owner: str, cols: int, rows: int) -> Shell:
        """This browser's shell: the one it has, or a new one."""
        with self._lock:
            shell = self._shells.get(owner)
            if shell is not None and shell.running():
                return shell
            self._drop_ended()
            if len(self._shells) >= self.limit:
                raise Refused("Too many shells are open on the box. Close one first.")
            env = {**os.environ, "TERM": "xterm-256color", "COLORTERM": "truecolor"}
            env.update(read_env_file(self.env_file))
            shell = Shell(self.argv, env=env, cwd=self.workdir, cols=cols, rows=rows)
            try:
                os.makedirs(self.workdir, exist_ok=True)
                shell.start()
            except OSError as failed:
                raise Refused(f"The shell couldn't start: {failed.strerror}.") from None
            self._shells[owner] = shell
            return shell

    def get(self, owner: str) -> Shell | None:
        """This browser's shell, if it has one (ended or not: its last output still shows)."""
        with self._lock:
            return self._shells.get(owner)

    def close(self, owner: str) -> None:
        """Close this browser's shell, if it has one."""
        with self._lock:
            shell = self._shells.pop(owner, None)
        if shell is not None:
            shell.close()

    def close_all(self) -> None:
        """Close every shell: every browser has been signed out."""
        with self._lock:
            shells, self._shells = list(self._shells.values()), {}
        for shell in shells:
            shell.close()

    def _drop_ended(self) -> None:
        """Forget shells that have exited. The caller holds the lock."""
        for owner in [o for o, s in self._shells.items() if not s.running()]:
            del self._shells[owner]

    def reap(self) -> None:
        """Close shells nobody has viewed for the idle limit, if nothing runs in them, and forget ended ones."""
        with self._lock:
            idle = [o for o, s in self._shells.items() if s.idle_for() > self.idle_seconds and not s.busy()]
        for owner in idle:
            self.close(owner)
        with self._lock:
            self._drop_ended()

    def _reap_loop(self) -> None:
        while True:
            time.sleep(60)
            self.reap()
