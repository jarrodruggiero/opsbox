"""Runs a program in a pseudo-terminal, keeps recent output, and streams new output to subscribers."""
from __future__ import annotations

import errno
import fcntl
import os
import pty
import queue
import select
import shutil
import signal
import struct
import subprocess
import sys
import termios
import threading
import time


# How much typed input may wait for a process that isn't reading it.
MAX_PENDING_INPUT = 65536

# A terminal program's one repaint is many writes: cursor up, erase, then line by
# line. They're kept together until BURST_QUIET seconds pass with nothing more, so
# a page can't draw the state in between, but for BURST_LIMIT seconds at most, so
# steady output still arrives as it goes. Both too short for anyone to notice.
BURST_QUIET = 0.015
BURST_LIMIT = 0.05

# Popen can't give a child its controlling terminal, and preexec_fn isn't safe
# with threads, so this small shim takes it and then becomes the program. Without
# one a resize signals nobody, Ctrl-C reaches nobody unless the program takes the
# terminal itself, as bash does, and when it exits nothing hangs the terminal up,
# so a job it left behind keeps the stream open and it looks alive for hours.
# Isolated (-I -S) so that settings like PYTHONHOME, which are the program's
# business, can't stop it starting.
_TAKE_TERMINAL = "import fcntl,os,sys,termios;fcntl.ioctl(0,termios.TIOCSCTTY,0);os.execvp(sys.argv[1],sys.argv[1:])"


class PtyProcess:
    """Runs a program in a pseudo-terminal (not fork, safe in a multi-threaded process)."""

    def __init__(
        self,
        argv: list[str],
        *,
        env: dict[str, str] | None = None,
        cwd: str | None = None,
        cols: int = 120,
        rows: int = 40,
        keep_bytes: int = 262144,
    ) -> None:
        self._argv = argv
        self._env = env
        self._cwd = cwd
        self._cols = cols
        self._rows = rows
        self._keep_bytes = keep_bytes
        self._buffer: bytearray = bytearray()
        self._subscribers: list[queue.Queue[bytes | None]] = []
        self._lock = threading.Lock()
        self.proc: subprocess.Popen[bytes] | None = None
        self._fd: int | None = None
        self._input: queue.Queue[bytes | None] = queue.Queue()
        self._pending = 0

    def start(self) -> None:
        """Start the process in a pseudo-terminal with the configured window size."""
        # Here, because past the shim a missing program would only be an exit code.
        if shutil.which(self._argv[0], path=(self._env or os.environ).get("PATH")) is None:
            raise FileNotFoundError(errno.ENOENT, os.strerror(errno.ENOENT), self._argv[0])
        master, slave = pty.openpty()
        try:
            fcntl.ioctl(slave, termios.TIOCSWINSZ, struct.pack("HHHH", self._rows, self._cols, 0, 0))
            self.proc = subprocess.Popen(
                [sys.executable, "-I", "-S", "-c", _TAKE_TERMINAL, *self._argv],
                stdin=slave,
                stdout=slave,
                stderr=slave,
                env=self._env,
                cwd=self._cwd,
                start_new_session=True,
                close_fds=True,
            )
        except BaseException:
            # A failed start (no such directory or program) must not leak the pair.
            os.close(master)
            os.close(slave)
            raise
        os.close(slave)
        self._fd = master
        threading.Thread(target=self._read_loop, daemon=True).start()
        threading.Thread(target=self._write_loop, daemon=True).start()

    def _read_burst(self) -> bytes:
        """One read, plus whatever follows it at once (see BURST_QUIET)."""
        chunk = os.read(self._fd, 65536)
        deadline = time.monotonic() + BURST_LIMIT
        while chunk and len(chunk) < 65536:
            wait = min(BURST_QUIET, deadline - time.monotonic())
            if wait <= 0 or not select.select([self._fd], [], [], wait)[0]:
                break
            try:
                more = os.read(self._fd, 65536)
            except OSError:
                # The process has gone. The next read says so again, and this
                # way what it wrote last isn't lost.
                break
            if not more:
                break
            chunk += more
        return chunk

    def _read_loop(self) -> None:
        """Read from the master fd, trim the buffer, and push chunks to subscribers."""
        while True:
            try:
                chunk = self._read_burst()
            except OSError:
                break
            if chunk == b"":
                break
            with self._lock:
                self._buffer.extend(chunk)
                if len(self._buffer) > self._keep_bytes:
                    self._buffer = self._buffer[len(self._buffer) - self._keep_bytes :]
                for q in self._subscribers:
                    q.put(chunk)
        with self._lock:
            for q in self._subscribers:
                q.put(None)
        self._input.put(None)

    def _write_loop(self) -> None:
        """Hand typed input to the process, and close the terminal once it has gone.

        A thread of its own, because a write blocks while a process isn't
        reading its input, and that must not hold a web request. It owns closing
        the descriptor, so a late write can't land on a number reused since.
        """
        while (data := self._input.get()) is not None:
            view = memoryview(data)
            try:
                while view:
                    view = view[os.write(self._fd, view):]
            except OSError:
                pass
            with self._lock:
                self._pending -= len(data)
        with self._lock:
            fd, self._fd = self._fd, None
        os.close(fd)

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

    def unsubscribe(self, q: queue.Queue[bytes | None]) -> None:
        """Remove a subscriber queue if present."""
        with self._lock:
            if q in self._subscribers:
                self._subscribers.remove(q)

    def write(self, data: bytes) -> None:
        """Queue input for the process; OSError if it has gone or has stopped reading."""
        if not self.running():
            raise OSError("the process has exited")
        with self._lock:
            if self._pending + len(data) > MAX_PENDING_INPUT:
                raise OSError("the process isn't reading its input")
            self._pending += len(data)
        self._input.put(data)

    def resize(self, cols: int, rows: int) -> None:
        """Change the terminal's size; the kernel tells the process."""
        with self._lock:
            if self._fd is not None:
                try:
                    fcntl.ioctl(self._fd, termios.TIOCSWINSZ, struct.pack("HHHH", rows, cols, 0, 0))
                except OSError:
                    pass

    def running(self) -> bool:
        """Return True while the process has not exited."""
        return self.proc is not None and self.proc.poll() is None

    def wait(self, timeout: float | None = None) -> int | None:
        """Wait for the process to exit, returning the exit code or None on timeout."""
        try:
            return self.proc.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            return None

    def busy(self) -> bool:
        """Whether something other than the program itself holds the terminal: a shell running a command."""
        with self._lock:
            if self._fd is None or not self.running():
                return False
            try:
                return os.tcgetpgrp(self._fd) != self.proc.pid
            except OSError:
                return False

    def hang_up(self) -> None:
        """End it the way closing a terminal does: an interactive shell ignores SIGTERM."""
        if self.running():
            try:
                os.killpg(self.proc.pid, signal.SIGHUP)
            except ProcessLookupError:
                pass
            self.wait(timeout=1)
            self.terminate(grace=1)

    def terminate(self, grace: float = 5.0) -> None:
        """Send SIGTERM to the process group, then SIGKILL after grace seconds."""
        if self.running():
            try:
                os.killpg(self.proc.pid, signal.SIGTERM)
            except ProcessLookupError:
                pass
            self.wait(timeout=grace)
            if self.running():
                try:
                    os.killpg(self.proc.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
