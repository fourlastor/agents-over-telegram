#!/usr/bin/env python3
"""Start this project's prepared Agents over Telegram runtime from a host plugin.

Host plugins are installed once, but each project selects its own runtime and
client. Setup writes a non-secret ``.aot-runtime.json`` in the exact project
folder. This launcher reads only that folder. It never searches parent folders,
reads credentials, or falls back to an ``aot`` found on PATH.

Usage: aot_host_launch.py {mcp,watch,hook} [arguments passed to aot]

It runs with any Python 3.8+ (for example the system python3), because the
prepared runtime's own interpreter is only known after reading the pointer.
"""
import json
import os
import stat
import sys
import time

POINTER = ".aot-runtime.json"
PROFILE = ".aot-profile.json"
COMMANDS = ("mcp", "watch", "hook")


def _project():
    for key in ("AOT_PROJECT_DIR", "CLAUDE_PROJECT_DIR"):
        value = os.environ.get(key, "")
        if value:
            return value
    return os.getcwd()


def _safe_owned(info):
    return (stat.S_ISREG(info.st_mode) and info.st_uid == os.getuid()
            and not info.st_mode & 0o022 and info.st_nlink == 1)


def _owned_regular(path):
    try:
        info = os.lstat(path)
    except OSError:
        return False
    return _safe_owned(info)


def _read_pointer(path):
    """Read the pointer without following a symlink, checking the opened file itself."""
    fd = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_CLOEXEC", 0))
    with os.fdopen(fd, "r", encoding="utf-8") as stream:
        if not _safe_owned(os.fstat(stream.fileno())):
            raise ValueError("unsafe pointer")
        raw = stream.read(65537)
    if len(raw) > 65536:
        raise ValueError("pointer too large")
    return json.loads(raw)


def _executable(project):
    """Return (executable, problem). problem is None, 'missing' or 'unusable'."""
    if not hasattr(os, "getuid"):
        return None, "unusable"
    path = os.path.join(project, POINTER)
    if not os.path.lexists(path):
        return None, "missing"
    if not _owned_regular(path) or not _owned_regular(os.path.join(project, PROFILE)):
        return None, "unusable"
    try:
        value = _read_pointer(path)
    except (OSError, ValueError):
        return None, "unusable"
    if (not isinstance(value, dict) or set(value) != {"version", "executable"}
            or type(value.get("version")) is not int or value.get("version") != 1):
        return None, "unusable"
    executable = value.get("executable")
    if not isinstance(executable, str) or not os.path.isabs(executable) or "\x00" in executable:
        return None, "unusable"
    try:
        info = os.stat(executable)
    except (OSError, ValueError):
        return None, "unusable"
    # The runtime belongs to this user (or root) and nobody else can replace it.
    if (not stat.S_ISREG(info.st_mode) or info.st_uid not in (os.getuid(), 0)
            or info.st_mode & 0o002 or not os.access(executable, os.X_OK)):
        return None, "unusable"
    return executable, None


def main(argv):
    if not argv or argv[0] not in COMMANDS:
        sys.stderr.write("Usage: aot_host_launch.py {mcp,watch,hook} [arguments]\n")
        return 2
    command = argv[0]
    # Launcher-only option: a host that reports a finished background command as
    # an event (Claude Code monitors) should not be woken in unrelated projects.
    idle = "--idle-if-unconfigured" in argv[1:]
    rest = [arg for arg in argv[1:] if arg != "--idle-if-unconfigured"]
    project = os.path.realpath(_project())
    executable, problem = _executable(project)
    if executable is None:
        if command == "mcp":
            if problem == "missing":
                sys.stderr.write("Agents over Telegram is not set up for this project folder. "
                                 "Run the telegram-setup skill, or 'aot setup --host ...' in this folder.\n")
            else:
                sys.stderr.write("The Agents over Telegram runtime recorded for this project folder is missing "
                                 "or unsafe. Check that .aot-runtime.json and .aot-profile.json are owned by you "
                                 "and not writable by others, then run 'aot setup --host ...' in this folder again.\n")
            return 2
        # Hooks and monitors stay silent in projects that do not use Telegram.
        if command == "hook":
            _drain_stdin()
        if command == "watch" and idle:
            try:
                while True:
                    time.sleep(3600)
            except KeyboardInterrupt:
                pass
        return 0
    os.chdir(project)
    args = [executable, command] + rest
    if command == "hook":
        args += ["--project-dir", project]
    try:
        os.execv(executable, args)
    except OSError:
        sys.stderr.write("The prepared Agents over Telegram runtime could not be started.\n")
        return 0 if command == "hook" else 2


def _drain_stdin():
    try:
        while sys.stdin.read(65536):
            pass
    except (OSError, ValueError, UnicodeError):
        pass


def _run(argv):
    # A hook must never block the host or print a traceback, whatever goes wrong.
    hook = bool(argv) and argv[0] == "hook"
    try:
        return main(argv)
    except SystemExit:
        if hook:
            return 0
        raise
    except KeyboardInterrupt:
        return 0 if hook else 130
    except BaseException:
        if hook:
            return 0
        sys.stderr.write("The Agents over Telegram launcher failed.\n")
        return 2


if __name__ == "__main__":
    sys.exit(_run(sys.argv[1:]))
