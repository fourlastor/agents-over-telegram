"""What differs between POSIX (Linux, macOS) and Windows, in one place.

POSIX keeps private state private with file modes and owners, and the core
checks both before trusting a file. Windows has neither: ``st_uid`` is 0 and
mode bits do not describe who can read a file. There the state directory
lives in the user's profile (%LOCALAPPDATA%), which other users cannot read
by default, and the checks that rely on modes and owners are skipped; the
checks that do not (regular file, single link, no symlink) still apply.
"""
from __future__ import annotations

import os
from pathlib import Path

WINDOWS = os.name == 'nt'

# Flags Windows does not have are no-ops there.
O_NOFOLLOW = getattr(os, 'O_NOFOLLOW', 0)
O_CLOEXEC = getattr(os, 'O_CLOEXEC', 0)
O_NONBLOCK = getattr(os, 'O_NONBLOCK', 0)
# Windows opens files in text mode unless told otherwise.
O_BINARY = getattr(os, 'O_BINARY', 0)


def getuid():
    """This user's id, or None on Windows."""
    return os.getuid() if hasattr(os, 'getuid') else None


def owned(info, *, root=False):
    """True when a stat result belongs to this user (or to root, when allowed); always on Windows."""
    if WINDOWS:
        return True
    return info.st_uid == os.getuid() or (root and info.st_uid == 0)


def private(info, mask=0o077):
    """True when no mode bit in ``mask`` is set; always on Windows, where modes say nothing."""
    return WINDOWS or not info.st_mode & mask


def mode_is(info, mode):
    """True when the permission bits are exactly ``mode``; always on Windows."""
    import stat
    return WINDOWS or stat.S_IMODE(info.st_mode) == mode


def lock(fd, *, blocking=False):
    """Take an exclusive lock on an open file; BlockingIOError when another process holds it.

    POSIX uses flock. Windows locks the file's first byte with msvcrt, which
    works past the end of an empty file and is released when the file closes.
    """
    if WINDOWS:
        import msvcrt
        os.lseek(fd, 0, os.SEEK_SET)
        while True:
            try:
                msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
                return
            except OSError:
                if not blocking:
                    raise BlockingIOError('locked') from None
                import time
                time.sleep(0.1)
    import fcntl
    fcntl.flock(fd, fcntl.LOCK_EX if blocking else fcntl.LOCK_EX | fcntl.LOCK_NB)


def unlock(fd):
    if WINDOWS:
        import msvcrt
        os.lseek(fd, 0, os.SEEK_SET)
        try:
            msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
        except OSError:
            pass
        return
    import fcntl
    fcntl.flock(fd, fcntl.LOCK_UN)


def venv_scripts(venv):
    """A virtual environment's executables directory: bin on POSIX, Scripts on Windows."""
    return Path(venv) / ('Scripts' if WINDOWS else 'bin')


def executable_name(name):
    """``aot`` becomes ``aot.exe`` on Windows."""
    return name + '.exe' if WINDOWS else name
