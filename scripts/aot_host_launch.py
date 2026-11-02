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

POINTER = ".aot-runtime.json"
PROFILE = ".aot-profile.json"
COMMANDS = ("mcp", "watch", "hook")


def _project():
    for key in ("AOT_PROJECT_DIR", "CLAUDE_PROJECT_DIR"):
        value = os.environ.get(key, "")
        if value:
            return value
    return os.getcwd()


def _owned_regular(path):
    try:
        info = os.lstat(path)
    except OSError:
        return False
    return (stat.S_ISREG(info.st_mode) and info.st_uid == os.getuid()
            and not info.st_mode & 0o022 and info.st_nlink == 1)


def _executable(project):
    path = os.path.join(project, POINTER)
    if not _owned_regular(path) or not _owned_regular(os.path.join(project, PROFILE)):
        return None
    try:
        with open(path, "r", encoding="utf-8") as stream:
            value = json.loads(stream.read(65536))
    except (OSError, ValueError):
        return None
    if not isinstance(value, dict) or set(value) != {"version", "executable"} or value.get("version") != 1:
        return None
    executable = value.get("executable")
    if not isinstance(executable, str) or not os.path.isabs(executable) or "\x00" in executable:
        return None
    if not os.path.isfile(executable) or not os.access(executable, os.X_OK):
        return None
    return executable


def main(argv):
    if not argv or argv[0] not in COMMANDS:
        sys.stderr.write("Usage: aot_host_launch.py {mcp,watch,hook} [arguments]\n")
        return 2
    command = argv[0]
    project = os.path.realpath(_project())
    executable = _executable(project)
    if executable is None:
        if command == "mcp":
            sys.stderr.write("Agents over Telegram is not set up for this project folder. "
                             "Run the telegram-setup skill, or 'aot setup --host ...' in this folder.\n")
            return 2
        # Hooks and monitors stay silent in projects that do not use Telegram.
        if command == "hook":
            sys.stdin.read()
        return 0
    os.chdir(project)
    args = [executable, command] + argv[1:]
    if command == "hook":
        args += ["--project-dir", project]
    try:
        os.execv(executable, args)
    except OSError:
        sys.stderr.write("The prepared Agents over Telegram runtime could not be started.\n")
        return 0 if command == "hook" else 2


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
