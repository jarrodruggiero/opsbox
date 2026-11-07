from __future__ import annotations

import os
import queue
import re
import threading
import time

from pathlib import Path

from . import claude
from .keys import read_env_file
from .ptyproc import PtyProcess

# What the page says for each state: never colour alone, always a word and a symbol.
STATE_LABELS = {
    "running": ("\u25cf", "Running"),
    "stopped": ("\u25a0", "Stopped"),
    "backing-off": ("\u25d0", "Restarting soon"),
    "asking": ("?", "Waiting for you"),
    "signed-out": ("\u25cb", "Not signed in"),
}

# How the CLI ends a yes-or-no question: "Trust …? [y/N] ", "Enable …? (y/n) ".
YES_OR_NO = re.compile(r"[\[(]y/n[\])]\s*$", re.IGNORECASE)

# The trust check refuses to ask at all in a terminal too small to show the
# question, so a phone-sized console never shrinks the real one below this.
MIN_COLS, MIN_ROWS = 80, 24


def pending_question(output: str) -> str | None:
    """The yes-or-no question the output ends on, or None once anything follows it."""
    lines = claude.strip_ansi(output).rstrip().replace("\r", "\n").split("\n")
    return lines[-1].strip() if YES_OR_NO.search(lines[-1]) else None


def detail_line(rc: dict) -> str:
    """The line under Remote Control's heading: how it has been doing, or nothing."""
    parts = []
    if rc.get("restarts"):
        parts.append(f"Restarted {rc['restarts']} times")
    if rc.get("last_exit") is not None:
        parts.append(f"last exit {rc['last_exit']}")
    return " \u00b7 ".join(parts)


# A repaint: back up N rows, erase from there down, draw. A few digits, because
# int() refuses thousands of them, and any program can print those.
REPAINT = re.compile(rb"\x1b\[(\d{1,4})A\x1b\[J")


def redraws_in_place(data: bytes) -> bool:
    """Whether *data* is a repaint that ends no lower than it began, so that doing it again changes nothing.

    More newlines than rows means a log line above the block, which would be a
    line of its own the second time. Fewer is allowed, for lines that wrap. None
    is an erase with nothing drawn, which erases further each time.
    """
    match = REPAINT.match(data)
    return match is not None and 0 < data.count(b"\n") <= int(match.group(1))


class Console:
    """What the page's terminal window shows: Remote Control's output and the manager's notes."""

    def __init__(self, *, keep_bytes: int = 262144, collapse_repaints: bool = True) -> None:
        self._buffer: bytearray = bytearray()
        self._collapse_repaints = collapse_repaints
        self._last_fed = b""
        self._keep_bytes = keep_bytes
        self._lock = threading.Lock()
        self._subscribers: list[queue.Queue[bytes | None]] = []
        self.closed = False

    def close(self) -> None:
        """End every viewer's stream: this console has no more to show."""
        with self._lock:
            self.closed = True
            for q in self._subscribers:
                try:
                    q.put_nowait(None)
                except queue.Full:
                    pass
            self._subscribers = []

    def feed(self, data: bytes) -> None:
        """Append data, trim from the front, and push to subscribers."""
        with self._lock:
            # With a session attached, Claude repaints its status block every
            # second. The same repaint with nothing since changes nothing on
            # screen, and would fill the reload history and `docker logs`.
            if self._collapse_repaints and data == self._last_fed and redraws_in_place(data):
                return
            self._last_fed = data
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
            if self.closed:
                q.put_nowait(None)
            else:
                self._subscribers.append(q)
            return bytes(self._buffer), q

    def subscribed(self, q: queue.Queue[bytes | None]) -> bool:
        """Whether *q* still receives output, or was dropped for falling behind."""
        with self._lock:
            return q in self._subscribers or self.closed

    def unsubscribe(self, q: queue.Queue[bytes | None]) -> None:
        """Remove a subscriber queue if present."""
        with self._lock:
            if q in self._subscribers:
                self._subscribers.remove(q)


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
        self.question: str | None = None
        self._cols, self._rows = 120, 40
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
        """Stop the loop and terminate the current process.

        The loop notes it when it ends: the process's last words move relative
        to the cursor, so a note now would put them in the wrong place.
        """
        with self._lock:
            self._want = False
            # Or a later crash would count as this restart, and skip its wait.
            self._restart_now = False
            if self._proc is not None and self._proc.running():
                self._proc.terminate()
            self.state = "stopped"
            self.question = None
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
        # As for stop: with a process to end, the loop notes it once it has gone.
        if running:
            self._proc.terminate()
        else:
            self.console.note("restarting")
        self._wake.set()
        return True

    def answer(self, yes: bool, question: str) -> bool:
        """Answer *question*, if it's the one Remote Control is asking now; False if not.

        Only a person's click or keystroke ever gets here: the manager never
        answers on its own. The text is what ties a click to what its owner read:
        a double-click must not answer the next question, unseen. No ends the
        process, and starting it again would only ask again, so No also stops it.
        """
        with self._lock:
            if self.question is None or self.question != question or self._proc is None or not self._proc.running():
                return False
            try:
                self._proc.write(b"y\r" if yes else b"n\r")
            except OSError:
                return False
            self.state = "running"
            self.question = None
        if not yes:
            self.stop()
        return True

    def write(self, data: bytes) -> bool:
        """Type into Remote Control's terminal; False if it isn't running or isn't reading."""
        with self._lock:
            proc = self._proc
        if proc is None or not proc.running():
            return False
        try:
            proc.write(data)
        except OSError:
            return False
        return True

    def resize(self, cols: int, rows: int) -> None:
        """Size the terminal to the page's, never below what the CLI needs to ask its questions."""
        with self._lock:
            self._cols, self._rows = max(cols, MIN_COLS), max(rows, MIN_ROWS)
            if self._proc is not None:
                self._proc.resize(self._cols, self._rows)

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
                "question": self.question,
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
                    # Stop may have been clicked during that check, which takes seconds.
                    if not self._want:
                        break
                    self.state = "signed-out"
                if problem != self._problem:
                    self.console.note(problem)
                self._problem = problem
                self._wake.wait(timeout=5.0)
                self._wake.clear()
                continue

            # b. Build env and start the process. Stop may have been clicked
            # while the sign-in was being checked, so look again under the lock.
            env = {"TERM": "xterm-256color", "COLORTERM": "truecolor", **os.environ}
            env.update(read_env_file(self.env_file))
            with self._lock:
                if not self._want:
                    break
                self._proc = PtyProcess(
                    [self.claude or claude.CLAUDE, "remote-control", *self.args],
                    env=env,
                    cwd=self.workdir,
                    cols=self._cols,
                    rows=self._rows,
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
                    # Together, so that a Stop can't land between them and go
                    # unheard for the whole wait.
                    if not self._want:
                        break
                    self.state = "backing-off"
                    self._wake.clear()
                self._wake.wait(timeout=self.backoff_max)
                continue
            self.console.note("starting Remote Control")

            # c. Pump the subscription queue.
            window: str = ""
            while True:
                try:
                    chunk = sub_q.get(timeout=1.0)
                except queue.Empty:
                    # On Linux a job left behind keeps the terminal open after the
                    # process has gone, so the stream alone might not end for hours.
                    if self._proc.running():
                        continue
                    break
                if chunk is None:
                    break
                self.console.feed(chunk)
                window += chunk.decode("utf-8", errors="replace")
                window = window[-4096:]
                # No note here, unlike every other state: it would land in the
                # middle of the prompt the person is about to type at.
                question = pending_question(window)
                with self._lock:
                    if question != self.question:
                        self.question = question
                        if question is not None and self.state == "running":
                            self.state = "asking"
                        elif question is None and self.state == "asking":
                            self.state = "running"

            # d. Process exited.
            code = self._proc.wait(5)
            self._proc.unsubscribe(sub_q)
            with self._lock:
                self.last_exit = code
                self.question = None
            self.console.note(f"Remote Control exited ({code})")

            # e. Decide whether to restart.
            with self._lock:
                if not self._want:
                    break
                restart_now, self._restart_now = self._restart_now, False
                if restart_now:
                    self.restarts += 1
            if restart_now:
                self.console.note("restarting")
                continue

            # A run that lasted counts as recovered, so the next wait starts short
            # again. Otherwise wait the current delay, then double it for next time.
            if time.time() - self.started_at >= self.stable_after:
                self._delay = self.backoff_start
            delay = self._delay
            self._delay = min(self._delay * 2, self.backoff_max)

            with self._lock:
                # Together, as above.
                if not self._want:
                    break
                self.state = "backing-off"
                self._wake.clear()
            self.console.note(f"starting again in {delay:g}s")
            self._wake.wait(timeout=delay)

            with self._lock:
                self.restarts += 1

        self.console.note("stopped")
