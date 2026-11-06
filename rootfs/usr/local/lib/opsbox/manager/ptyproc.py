"""Runs a program in a pseudo-terminal, keeps recent output, and streams new output to subscribers."""
from __future__ import annotations

import fcntl
import os
import pty
import queue
import signal
import struct
import subprocess
import termios
import threading


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

    def start(self) -> None:
        """Start the process in a pseudo-terminal with the configured window size."""
        master, slave = pty.openpty()
        try:
            fcntl.ioctl(slave, termios.TIOCSWINSZ, struct.pack("HHHH", self._rows, self._cols, 0, 0))
            self.proc = subprocess.Popen(
                self._argv,
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
        t = threading.Thread(target=self._read_loop, daemon=True)
        t.start()

    def _read_loop(self) -> None:
        """Read from the master fd, trim the buffer, and push chunks to subscribers."""
        while True:
            try:
                chunk = os.read(self._fd, 65536)
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
        os.close(self._fd)
        self._fd = None

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
        """Write data to the pseudo-terminal stdin."""
        if not self.running():
            # OSError, as a write to a closed pty would raise, so one except covers both.
            raise OSError("the process has exited")
        os.write(self._fd, data)

    def running(self) -> bool:
        """Return True while the process has not exited."""
        return self.proc is not None and self.proc.poll() is None

    def wait(self, timeout: float | None = None) -> int | None:
        """Wait for the process to exit, returning the exit code or None on timeout."""
        try:
            return self.proc.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            return None

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
