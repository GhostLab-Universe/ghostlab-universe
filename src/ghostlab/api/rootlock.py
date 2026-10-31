"""One server process per storage root, enforced by the operating system.

The controller's guarantees assume one job per campaign. A second process on the same
root breaks them: both can start a campaign for one idempotency key, and the loser of an
approval race looks like crash recovery to ``_consumed_here``, so an approved repair runs
twice. The job registry can't see across processes, so the root is locked instead.

The operating system releases the lock when the process ends, however it ends. A crash
can't block a restart, and the leftover ``server.lock`` file means nothing by itself.
"""

import os
from pathlib import Path

LOCK_NAME = "server.lock"

if os.name == "nt":
    import msvcrt

    def _lock(fd: int) -> None:
        os.lseek(fd, 0, os.SEEK_SET)
        msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)

    def _unlock(fd: int) -> None:
        os.lseek(fd, 0, os.SEEK_SET)
        msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
else:
    import fcntl

    def _lock(fd: int) -> None:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)

    def _unlock(fd: int) -> None:
        fcntl.flock(fd, fcntl.LOCK_UN)


class RootLocked(Exception):
    """Another server holds this storage root."""


class RootLock:
    def __init__(self, root: Path):
        self.root, self._fd = Path(root), None

    def __enter__(self) -> "RootLock":
        self.root.mkdir(parents=True, exist_ok=True)
        fd = os.open(self.root / LOCK_NAME, os.O_RDWR | os.O_CREAT, 0o600)
        try:
            _lock(fd)
        except OSError as error:
            os.close(fd)
            raise RootLocked(f"another server is using {self.root}; run one server per "
                             "storage root") from error
        self._fd = fd
        return self

    def __exit__(self, *_) -> None:
        fd, self._fd = self._fd, None
        try:
            _unlock(fd)
        finally:
            os.close(fd)
