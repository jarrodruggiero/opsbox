from __future__ import annotations

import os
import queue
import re
import threading
import time

from pathlib import Path

from . import claude
from .ptyproc import PtyProcess

TRUST = re.compile(r"Trust .+\?\s*\[y/N\]")


class Console:
    """What the page's terminal window shows: Remote Control's output and the manager's notes."""

    def __init__(self, *, keep_bytes: int = 262144) -> None:
        self._buffer: bytearray = bytearray()
        self._keep_bytes = keep_bytes
        self._lock = threading.Lock()
        self._subscribers: list[queue.Queue[bytes | None]] = []

    def feed(self, data: bytes) -> None:
        """Append data, trim from the front, and push to subscribers."""
        with self._lock:
            self._buffer.extend(data)
            if len(self._buffer) > self._keep_bytes:
                self._buffer = self._buffer[len(self._buffer) - self._keep_bytes :]
            for q in list(self._subscribers):
                try:
                    q.put_nowait(data)
                except queue.Full:
                    # A viewer that stopped reading is dropped rather than
                    # left to grow without bound; its page reconnects with a
                    # fresh backlog.
                    self._subscribers.remove(q)

    def note(self, text: str) -> None:
        """Write a one-line note with a dim prefix to the console."""
        self.feed(f"\r\n\x1b[2m[opsbox] {text}\x1b[0m\r\n".encode())

    def output(self) -> bytes:
        """Return a copy of the kept buffer."""
        with self._lock:
            return bytes(self._buffer)

    def subscribe(self) -> queue.Queue[bytes | None]:
        """Create and return a new subscriber queue."""
        q: queue.Queue[bytes | None] = queue.Queue()
        with self._lock:
            self._subscribers.append(q)
        return q

    def attach(self, limit: int = 1000) -> tuple[bytes, queue.Queue[bytes | None]]:
        """Take the backlog and subscribe in one step, so a new viewer neither misses nor repeats a chunk.

        The queue holds at most *limit* chunks; a viewer that falls that far
        behind is dropped (see feed).
        """
        q: queue.Queue[bytes | None] = queue.Queue(maxsize=limit)
        with self._lock:
            self._subscribers.append(q)
            return bytes(self._buffer), q

    def subscribed(self, q: queue.Queue[bytes | None]) -> bool:
        """Whether *q* still receives output, or was dropped for falling behind."""
        with self._lock:
            return q in self._subscribers

    def unsubscribe(self, q: queue.Queue[bytes | None]) -> None:
        """Remove a subscriber queue if present."""
        with self._lock:
            if q in self._subscribers:
                self._subscribers.remove(q)


def read_env_file(path: Path) -> dict[str, str]:
    """Parse lines of the form NAME=VALUE from a file.

    Skip blank lines and lines starting with '#'.  Split on the first '='.
    Return {} if the file is missing.
    """
    result: dict[str, str] = {}
    try:
        with open(path) as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#"):
                    continue
                if "=" not in line:
                    continue
                key, value = line.split("=", 1)
                result[key] = value
    except FileNotFoundError:
        pass
    return result


class RemoteControl:
    """Keep `claude remote-control` running in a pseudo-terminal with backoff restart."""

    def __init__(
        self,
        console: Console,
        *,
        workdir: str,
        args: list[str],
        env_file: Path,
        claude: str | None = None,
        owner_file: Path | None = None,
        backoff_start: float = 5.0,
        backoff_max: float = 60.0,
        stable_after: float = 300.0,
    ) -> None:
        self.console = console
        self.workdir = workdir
        self._problem: str | None = None
        self.args = args
        self.env_file = env_file
        self.claude = claude
        self.owner_file = owner_file
        self.backoff_start = backoff_start
        self.backoff_max = backoff_max
        self.stable_after = stable_after

        self.state: str = "stopped"
        self.restarts: int = 0
        self.last_exit: int | None = None
        self.started_at: float | None = None
        self._proc: PtyProcess | None = None
        self._want: bool = False
        self._lock = threading.Lock()
        self._wake = threading.Event()
        self._thread: threading.Thread | None = None
        self._delay: float = backoff_start
        self._restart_now: bool = False

    def start(self) -> None:
        """Request the loop to start (or keep running)."""
        with self._lock:
            self._want = True
            if self._thread is None or not self._thread.is_alive():
                self._thread = threading.Thread(target=self._loop, daemon=True)
                self._thread.start()
            self._wake.set()

    def stop(self) -> None:
        """Stop the loop and terminate the current process."""
        with self._lock:
            self._want = False
            if self._proc is not None and self._proc.running():
                self._proc.terminate()
            self.state = "stopped"
        self.console.note("stopped")
        self._wake.set()

    def restart(self) -> bool:
        """Restart now: end the running process, or cut a backoff wait short.

        False, and nothing changes, when there is nothing to restart (stopped,
        or waiting for a sign-in), so a later crash still backs off.
        """
        with self._lock:
            running = self._proc is not None and self._proc.running()
            if not running and self.state != "backing-off":
                return False
            if running:
                self._restart_now = True
        self.console.note("restarting")
        if running:
            self._proc.terminate()
        self._wake.set()
        return True

    def answer_trust(self) -> bool:
        """Answer the trust question if the process is asking one.

        Returns True only when the state is "trust-asked" and the process is running,
        after writing "y\r" to the pty, setting state "running", and noting.
        """
        with self._lock:
            if self.state == "trust-asked" and self._proc is not None and self._proc.running():
                self._proc.write(b"y\r")
                self.state = "running"
                self.console.note("trusted the directory")
                return True
        return False

    def status(self) -> dict:
        """Return a snapshot of the current status."""
        with self._lock:
            pid = self._proc.proc.pid if self._proc is not None and self._proc.proc is not None else None
            return {
                "state": self.state,
                "restarts": self.restarts,
                "last_exit": self.last_exit,
                "started_at": self.started_at,
                "pid": pid,
                "workdir": str(self.workdir),
            }

    def _loop(self) -> None:
        """Main loop: check auth, start, pump, backoff, repeat."""
        while True:
            with self._lock:
                if not self._want:
                    break

            # a. Check the sign-in can start Remote Control, and say why not once
            # per reason, not every 5 seconds.
            owner = claude.read_owner(self.owner_file) if self.owner_file else None
            problem = claude.problem(claude.auth_status(claude=self.claude), owner)
            if problem is not None:
                with self._lock:
                    self.state = "signed-out"
                if problem != self._problem:
                    self.console.note(problem)
                self._problem = problem
                self._wake.wait(timeout=5.0)
                self._wake.clear()
                continue

            # b. Build env and start the process. Stop may have been clicked
            # while the sign-in was being checked, so look again under the lock.
            env = dict(os.environ)
            env.update(read_env_file(self.env_file))
            with self._lock:
                if not self._want:
                    break
                self._proc = PtyProcess(
                    [self.claude or claude.CLAUDE, "remote-control", *self.args],
                    env=env,
                    cwd=self.workdir,
                )
                sub_q = self._proc.subscribe()
                try:
                    # A session may have deleted the directory since the last start.
                    os.makedirs(self.workdir, exist_ok=True)
                    self._proc.start()
                except (OSError, ValueError) as failed:
                    # ValueError: a NUL in the environment. Either way, say so
                    # and back off, rather than let this thread die silently.
                    self._proc = None
                    start_problem = f"couldn't start Remote Control: {failed}"
                else:
                    start_problem = None
                    self._problem = None
                    self.started_at = time.time()
                    self.state = "running"
            if start_problem is not None:
                if start_problem != self._problem:
                    self.console.note(start_problem)
                self._problem = start_problem
                with self._lock:
                    self.state = "backing-off"
                self._wake.clear()
                self._wake.wait(timeout=self.backoff_max)
                continue
            self.console.note("starting Remote Control")

            # c. Pump the subscription queue.
            window: str = ""
            while True:
                try:
                    chunk = sub_q.get()
                except Exception:
                    break
                if chunk is None:
                    break
                self.console.feed(chunk)
                window += chunk.decode("utf-8", errors="replace")
                window = window[-4096:]
                with self._lock:
                    if TRUST.search(claude.strip_ansi(window)) and self.state == "running":
                        self.state = "trust-asked"
                        self.console.note("asked to trust the directory: answer it on the page")
                        window = ""

            # d. Process exited.
            code = self._proc.wait(5)
            self._proc.unsubscribe(sub_q)
            with self._lock:
                self.last_exit = code
            self.console.note(f"Remote Control exited ({code})")

            # e. Decide whether to restart.
            with self._lock:
                if not self._want:
                    break
                if self._restart_now:
                    self._restart_now = False
                    self.restarts += 1
                    continue

            # A run that lasted counts as recovered, so the next wait starts short
            # again. Otherwise wait the current delay, then double it for next time.
            if time.time() - self.started_at >= self.stable_after:
                self._delay = self.backoff_start
            delay = self._delay
            self._delay = min(self._delay * 2, self.backoff_max)

            with self._lock:
                self.state = "backing-off"
            self.console.note(f"starting again in {delay:g}s")

            self._wake.clear()
            self._wake.wait(timeout=delay)

            with self._lock:
                self.restarts += 1
