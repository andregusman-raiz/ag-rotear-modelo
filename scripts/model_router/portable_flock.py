"""Small fcntl-compatible advisory lock facade.

POSIX uses fcntl.flock. Windows does not ship fcntl, so this module exposes the
subset used by the router through msvcrt.locking.
"""

import errno
import os


try:
    import fcntl as _native_fcntl
except ImportError:  # pragma: no cover - exercised on Windows.
    _native_fcntl = None


LOCK_EX = 1
LOCK_UN = 8
LOCK_NB = 4


class _PortableFcntl:
    LOCK_EX = getattr(_native_fcntl, "LOCK_EX", LOCK_EX)
    LOCK_UN = getattr(_native_fcntl, "LOCK_UN", LOCK_UN)
    LOCK_NB = getattr(_native_fcntl, "LOCK_NB", LOCK_NB)

    def flock(self, descriptor: int, operation: int) -> None:
        if _native_fcntl is not None:
            _native_fcntl.flock(descriptor, operation)
            return
        self._lock_windows(descriptor, operation)

    def _lock_windows(self, descriptor: int, operation: int) -> None:
        import msvcrt

        unlock = bool(operation & self.LOCK_UN)
        nonblocking = bool(operation & self.LOCK_NB)
        mode = (
            msvcrt.LK_UNLCK
            if unlock
            else msvcrt.LK_NBLCK if nonblocking else msvcrt.LK_LOCK
        )
        try:
            position = os.lseek(descriptor, 0, os.SEEK_CUR)
        except OSError:
            position = 0
        try:
            os.lseek(descriptor, 0, os.SEEK_SET)
            if not unlock and os.fstat(descriptor).st_size == 0:
                os.write(descriptor, b"\x00")
                os.fsync(descriptor)
                os.lseek(descriptor, 0, os.SEEK_SET)
            msvcrt.locking(descriptor, mode, 1)
        except OSError as error:
            if nonblocking and error.errno in (
                errno.EACCES,
                errno.EAGAIN,
                getattr(errno, "EDEADLK", errno.EAGAIN),
            ):
                raise OSError(errno.EAGAIN, "file lock is busy") from None
            raise
        finally:
            try:
                os.lseek(descriptor, position, os.SEEK_SET)
            except OSError:
                pass


fcntl = _PortableFcntl()
