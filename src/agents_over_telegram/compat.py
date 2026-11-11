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
    fd = fd.fileno() if hasattr(fd, 'fileno') else fd
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
    fd = fd.fileno() if hasattr(fd, 'fileno') else fd
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


def pid_alive(pid):
    """True while a process with this id exists.

    POSIX asks with signal 0. On Windows os.kill(pid, 0) terminates the
    process instead, so ask the kernel for its exit code.
    """
    if WINDOWS:
        import ctypes
        from ctypes import wintypes
        kernel32 = ctypes.WinDLL('kernel32', use_last_error=True)
        kernel32.OpenProcess.restype = wintypes.HANDLE
        handle = kernel32.OpenProcess(0x1000, False, int(pid))  # PROCESS_QUERY_LIMITED_INFORMATION
        if not handle:
            # Access denied still means it exists; anything else means it is gone.
            return ctypes.get_last_error() == 5
        try:
            code = wintypes.DWORD()
            if not kernel32.GetExitCodeProcess(handle, ctypes.byref(code)):
                return True
            return code.value == 259  # STILL_ACTIVE
        finally:
            kernel32.CloseHandle(handle)
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except OSError:
        return True
    return True


def refuse_link(path):
    """Raise OSError when ``path`` is a symlink, where opening cannot refuse one itself.

    POSIX opens with O_NOFOLLOW. Windows has no such flag, so check first;
    the window between the check and the open needs write access to the
    user's own private directory.
    """
    if WINDOWS and os.path.islink(path):
        raise OSError('symlink refused')


def own_aot():
    """This runtime's aot executable: next to its Python, aot.exe on Windows."""
    import sys
    return Path(sys.executable).absolute().parent / executable_name('aot')
