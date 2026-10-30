"""OS locks shared by CLI, desktop and independent worker processes."""
from contextlib import contextmanager
from pathlib import Path
import os
import time


class LockWaitCancelled(Exception):
    """Cancelled before entering the protected operation."""


@contextmanager
def file_lock(path, wait):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open('a+') as handle:
        if os.name == 'nt':
            import msvcrt
            handle.seek(0)
            try:
                msvcrt.locking(handle.fileno(), msvcrt.LK_LOCK if wait else msvcrt.LK_NBLCK, 1)
            except OSError:
                yield False
                return
        else:
            import fcntl
            try:
                fcntl.flock(handle, fcntl.LOCK_EX | (0 if wait else fcntl.LOCK_NB))
            except OSError:
                yield False
                return
        yield True


@contextmanager
def waiting_lock(path, timeout, *, cancel=None, on_wait=None, on_acquired=None):
    """Bounded, cancellable waiting without Windows LK_LOCK's short retry limit."""
    if timeout < 0:
        raise ValueError('Lock timeout must be nonnegative.')
    deadline = time.monotonic() + timeout
    notified = False
    while True:
        if cancel and cancel():
            raise LockWaitCancelled()
        with file_lock(path, False) as locked:
            if locked:
                if cancel and cancel():
                    raise LockWaitCancelled()
                if on_acquired:
                    on_acquired()
                yield True
                return
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            yield False
            return
        if on_wait and not notified:
            on_wait()
            notified = True
        time.sleep(min(0.05, remaining))
