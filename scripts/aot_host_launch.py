#!/usr/bin/env python3
"""Start this project's prepared Agents over Telegram runtime from a host plugin.

Host plugins are installed once, but each project selects its own runtime and
client. Setup writes a non-secret ``.aot-runtime.json`` in the exact project
folder. This launcher reads only that folder. It never searches parent folders,
reads credentials, or falls back to an ``aot`` found on PATH.

Usage: aot_host_launch.py {mcp,watch,hook,wake} [arguments passed to aot]

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
COMMANDS = ("mcp", "watch", "hook", "wake")
# These run as host hooks: a failure must never block or wake the host.
HOOK_COMMANDS = ("hook", "wake")


MAX_HOOK_INPUT = 1024 * 1024


def _project(hook_input=None):
    for key in ("AOT_PROJECT_DIR", "CLAUDE_PROJECT_DIR"):
        value = os.environ.get(key, "")
        if value:
            return value
    # Codex documents the session folder as "cwd" in every hook input.
    if hook_input:
        try:
            cwd = json.loads(hook_input).get("cwd")
        except (ValueError, AttributeError, UnicodeError):
            cwd = None
        if isinstance(cwd, str) and os.path.isabs(cwd) and "\x00" not in cwd:
            return cwd
    return os.getcwd()


NOTICE = b"[Agents over Telegram] "
MAX_NOTICE = 4096


def _wake(args, project, hook_input):
    """Run the waiter and pass on exit status 2 only with a real notice.

    Exit status 2 wakes the host session. A runtime that predates ``aot wake``
    rejects the command with status 2 and a usage message, which would wake the
    session in a loop. So this launcher stays as the parent and checks the
    result instead of replacing itself with the runtime.
    """
    import subprocess
    args = args + ["--project-dir", project, "--host-pid", str(os.getppid())]
    session = _session(hook_input)
    if session:
        args += ["--session", session]
    try:
        result = subprocess.run(args, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                stderr=subprocess.PIPE)
    except OSError:
        return 0
    notice = result.stderr[:MAX_NOTICE]
    if result.returncode != 2 or not notice.startswith(NOTICE):
        return 0
    sys.stderr.buffer.write(notice)
    sys.stderr.flush()
    return 2


def _session(hook_input):
    """The host session id from hook input, or None if absent or unusual."""
    try:
        value = json.loads(hook_input).get("session_id")
    except (ValueError, AttributeError, UnicodeError, TypeError):
        return None
    if isinstance(value, str) and 0 < len(value) <= 128 and all(
            c.isascii() and (c.isalnum() or c in "-_") for c in value):
        return value
    return None


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
        if argv and argv[0] in HOOK_COMMANDS:
            return 0
        sys.stderr.write("Usage: aot_host_launch.py {mcp,watch,hook,wake} [arguments]\n")
        return 2
    command = argv[0]
    # Launcher-only option: a host that reports a finished background command as
    # an event (Claude Code monitors) should not be woken in unrelated projects.
    idle = "--idle-if-unconfigured" in argv[1:]
    rest = [arg for arg in argv[1:] if arg != "--idle-if-unconfigured"]
    hook_input = None
    if command in HOOK_COMMANDS:
        hook_input = sys.stdin.buffer.read(MAX_HOOK_INPUT + 1)
        if len(hook_input) > MAX_HOOK_INPUT:
            return 0
    project = os.path.realpath(_project(hook_input))
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
        # The input was already read to find the project, so pass it on.
        import subprocess
        result = subprocess.run(args + ["--project-dir", project], input=hook_input,
                                stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                                # Stay below the shortest host hook timeout (Codex: 20 s).
                                timeout=18)
        if result.returncode == 0 and result.stdout:
            sys.stdout.buffer.write(result.stdout)
            sys.stdout.flush()
        return 0
    if command == "wake":
        return _wake(args, project, hook_input)
    try:
        os.execv(executable, args)
    except OSError:
        sys.stderr.write("The prepared Agents over Telegram runtime could not be started.\n")
        return 2


def _run(argv):
    # A hook must never block the host or print a traceback, whatever goes wrong.
    hook = bool(argv) and argv[0] in HOOK_COMMANDS
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
