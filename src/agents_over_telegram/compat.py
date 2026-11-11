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


# msvcrt.locking reports a byte another process holds as EACCES, or EDEADLOCK
# after its internal retries.
_LOCK_CONTENTION = {13, 36}


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
            except OSError as error:
                # Only contention means "held by someone else"; a bad file
                # descriptor or a broken handle is a real error.
                if error.errno not in _LOCK_CONTENTION:
                    raise
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
        # Declared, so 64-bit handles are passed whole.
        kernel32.OpenProcess.argtypes = (wintypes.DWORD, wintypes.BOOL, wintypes.DWORD)
        kernel32.OpenProcess.restype = wintypes.HANDLE
        kernel32.GetExitCodeProcess.argtypes = (wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD))
        kernel32.GetExitCodeProcess.restype = wintypes.BOOL
        kernel32.CloseHandle.argtypes = (wintypes.HANDLE,)
        kernel32.CloseHandle.restype = wintypes.BOOL
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
    if not WINDOWS:
        return
    info = os.lstat(path)
    # Symlinks, junctions and every other reparse point redirect the open.
    import stat
    if stat.S_ISLNK(info.st_mode) or getattr(info, 'st_file_attributes', 0) & 0x400:  # FILE_ATTRIBUTE_REPARSE_POINT
        raise OSError('link or reparse point refused')


def own_aot():
    """This runtime's aot executable: next to its Python, aot.exe on Windows."""
    import sys
    return Path(sys.executable).absolute().parent / executable_name('aot')


_SID = None


def restrict_directory(path, run=None):
    """On Windows, give a private folder an ACL of its own: full control for this
    user and SYSTEM, nothing inherited. True when applied, False when it failed.

    POSIX folders are private by mode (0700) instead, so this does nothing there.
    Windows folders inherit their parent's ACL, which outside the user's profile
    (a state directory given with --state-dir, say) can let other users read
    the bot token.
    """
    if not WINDOWS:
        return True
    import subprocess
    global _SID
    if run is None:
        # The SID never changes for this process; ask whoami once.
        if _SID is None:
            _SID = _user_sid(subprocess.run)
        sid, run = _SID, subprocess.run
    else:
        sid = _user_sid(run)
    if sid is None:
        return False
    try:
        # By SID, not by name, so a renamed account or a name that is also a
        # group cannot pick up the grant.
        result = run(['icacls', str(path), '/inheritance:r', '/grant:r', f'*{sid}:(OI)(CI)F',
                      '*S-1-5-18:(OI)(CI)F'], capture_output=True, check=False)
    except (OSError, subprocess.SubprocessError):
        return False
    return getattr(result, 'returncode', 1) == 0


def _user_sid(run):
    """This user's SID (S-1-5-21-...), from whoami, or None."""
    import re
    import subprocess
    try:
        result = run(['whoami', '/user', '/fo', 'csv', '/nh'], capture_output=True, text=True, check=False)
    except (OSError, subprocess.SubprocessError):
        return None
    match = re.search(r'"(S-1-[0-9-]+)"', getattr(result, 'stdout', '') or '')
    return match.group(1) if match and getattr(result, 'returncode', 1) == 0 else None
