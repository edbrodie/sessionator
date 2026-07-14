"""flock-based single-writer locking (T-005 mechanical details).

Two lock uses, both built on ``fcntl.flock``:

* the store-write lock — held for the short critical section around a
  load-mutate-write of the store, taken blocking (with a timeout) by reconcile
  and by the backfill's write-back;
* the backfill single-instance lock — taken non-blocking at backfill start so a
  second backfill exits immediately rather than piling up.

Reads never take a lock (the store is append-safe and rewritten atomically).
"""

from __future__ import annotations

import errno
import fcntl
import os
import time


class LockBusy(Exception):
    """Raised when a non-blocking lock is already held by another process."""


class FileLock:
    """A flock over a lockfile. Use as a context manager.

    ``blocking=True`` waits up to ``timeout`` seconds (polling, since flock has
    no portable timed wait) then raises ``LockBusy``. ``blocking=False`` raises
    ``LockBusy`` immediately if the lock is held.
    """

    def __init__(self, path: str, *, blocking: bool = True, timeout: float = 600.0):
        self.path = path
        self.blocking = blocking
        self.timeout = timeout
        self._fd = None

    def acquire(self) -> "FileLock":
        os.makedirs(os.path.dirname(self.path), exist_ok=True)
        # O_RDWR|O_CREAT: flock needs an open fd; the file's contents are unused.
        self._fd = os.open(self.path, os.O_RDWR | os.O_CREAT, 0o644)
        if not self.blocking:
            self._flock_nb()
            return self
        deadline = time.monotonic() + self.timeout
        delay = 0.05
        while True:
            try:
                fcntl.flock(self._fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                return self
            except OSError as e:
                if e.errno not in (errno.EAGAIN, errno.EACCES, errno.EWOULDBLOCK):
                    self._close()
                    raise
                if time.monotonic() >= deadline:
                    self._close()
                    raise LockBusy(f"timed out waiting for lock: {self.path}")
                time.sleep(min(delay, 1.0))
                delay = min(delay * 1.5, 1.0)

    def _flock_nb(self):
        try:
            fcntl.flock(self._fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as e:
            self._close()
            if e.errno in (errno.EAGAIN, errno.EACCES, errno.EWOULDBLOCK):
                raise LockBusy(f"lock held: {self.path}") from None
            raise

    def release(self):
        if self._fd is not None:
            try:
                fcntl.flock(self._fd, fcntl.LOCK_UN)
            finally:
                self._close()

    def _close(self):
        if self._fd is not None:
            try:
                os.close(self._fd)
            finally:
                self._fd = None

    def __enter__(self):
        return self.acquire()

    def __exit__(self, *exc):
        self.release()
        return False
