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


RELAY_INIT_TIMEOUT = 30
RELAY_STOP_TIMEOUT = 5
RUNTIME_STOPPED = "The Agents over Telegram runtime stopped. Retry the call."


def _key(value):
    return json.dumps(value, sort_keys=True)


def _messages(line):
    """The JSON-RPC objects in one stdio line, or [] when it is not JSON."""
    try:
        value = json.loads(line)
    except (ValueError, UnicodeError):
        return []
    items = value if isinstance(value, list) else [value]
    return [item for item in items if isinstance(item, dict)]


class _Relay:
    """Keep one MCP connection to the host across runtime restarts.

    The host starts this launcher once per session. Instead of replacing itself
    with the runtime, it relays stdio to a runtime child. When the project's
    .aot-runtime.json names a different executable, or the child has stopped,
    the next request starts a new child, replays the host's initialize and
    initialized messages to it, and tells the host that the tool list changed.
    A swap waits until no host request is in flight, so no answer is lost.
    """

    def __init__(self, project, rest):
        import subprocess
        import threading
        self._subprocess = subprocess
        self.project = project
        self.rest = rest
        self.lock = threading.Lock()
        self.out_lock = threading.Lock()
        self.child = None
        self.executable = None
        self.pending = set()
        self.init_line = None
        self.init_key = None
        self.initialized_line = None
        self.replay_key = None
        self.replay_done = threading.Event()
        self.replay_ok = False
        self.replays = 0

    def _upstream(self, line):
        with self.out_lock:
            sys.stdout.buffer.write(line if line.endswith(b"\n") else line + b"\n")
            sys.stdout.buffer.flush()

    def _error(self, key):
        self._upstream(json.dumps({"jsonrpc": "2.0", "id": json.loads(key),
                                   "error": {"code": -32603, "message": RUNTIME_STOPPED}}).encode())

    def _start(self):
        import threading
        executable, _problem = _executable(self.project)
        if executable is None:
            raise OSError("runtime unavailable")
        child = self._subprocess.Popen([executable, "mcp"] + self.rest, cwd=self.project,
                                       stdin=self._subprocess.PIPE, stdout=self._subprocess.PIPE, bufsize=0)
        with self.lock:
            self.child, self.executable = child, executable
        threading.Thread(target=self._read_child, args=(child,), daemon=True).start()

    def _stop(self):
        with self.lock:
            child, self.child = self.child, None
        if child is None:
            return
        try:
            child.stdin.close()
        except OSError:
            pass
        try:
            child.wait(timeout=RELAY_STOP_TIMEOUT)
        except self._subprocess.TimeoutExpired:
            child.kill()
            child.wait()

    def _read_child(self, child):
        for line in iter(child.stdout.readline, b""):
            forward = True
            with self.lock:
                if child is not self.child:
                    return
                for message in _messages(line):
                    if "method" in message or "id" not in message:
                        continue
                    key = _key(message["id"])
                    if key == self.replay_key:
                        forward = False
                        self.replay_ok = "result" in message
                        self.replay_done.set()
                    elif key in self.pending:
                        self.pending.discard(key)
                        if key == self.init_key and isinstance(message.get("result"), dict):
                            # The relay announces tool list changes after a swap.
                            tools = message["result"].setdefault("capabilities", {}).setdefault("tools", {})
                            if isinstance(tools, dict):
                                tools["listChanged"] = True
                                line = json.dumps(message).encode()
            if forward:
                self._upstream(line)
        with self.lock:
            if child is not self.child:
                return
            self.child = None
            lost, self.pending = self.pending, set()
            self.replay_done.set()
        for key in sorted(lost):
            self._error(key)

    def _restart(self):
        """Start a fresh child and bring it to the host's session state."""
        self._stop()
        self._start()
        if self.init_line is None:
            return True
        self.replays += 1
        message = json.loads(self.init_line)
        message["id"] = "aot-relay-replay-%d" % self.replays
        with self.lock:
            self.replay_key = _key(message["id"])
            self.replay_done.clear()
            self.replay_ok = False
            child = self.child
        try:
            child.stdin.write(json.dumps(message).encode() + b"\n")
            child.stdin.flush()
        except (OSError, AttributeError):
            return False
        if not self.replay_done.wait(RELAY_INIT_TIMEOUT) or not self.replay_ok:
            self._stop()
            return False
        if self.initialized_line is not None:
            try:
                child.stdin.write(self.initialized_line)
                child.stdin.flush()
            except OSError:
                return False
        self._upstream(b'{"jsonrpc":"2.0","method":"notifications/tools/list_changed"}')
        return True

    def _needs_restart(self):
        with self.lock:
            if self.child is None:
                return True
            if self.pending:
                return False
            current = self.executable
        executable, _problem = _executable(self.project)
        return executable is not None and executable != current

    def run(self):
        try:
            self._start()
        except OSError:
            sys.stderr.write("The prepared Agents over Telegram runtime could not be started.\n")
            return 2
        for line in iter(sys.stdin.buffer.readline, b""):
            if not line.endswith(b"\n"):
                line += b"\n"
            messages = _messages(line)
            requests = [m for m in messages if "method" in m and "id" in m]
            initialize = any(m.get("method") == "initialize" for m in requests)
            if any(m.get("method") == "notifications/initialized" for m in messages):
                self.initialized_line = line
            if requests and self._needs_restart():
                try:
                    if initialize:
                        # A new session's initialize is forwarded as is, never replayed.
                        self._stop()
                        self._start()
                        ready = True
                    else:
                        ready = self._restart()
                except OSError:
                    ready = False
                if not ready:
                    for message in requests:
                        self._error(_key(message["id"]))
                    continue
            with self.lock:
                child = self.child
                for message in requests:
                    self.pending.add(_key(message["id"]))
                    if message.get("method") == "initialize":
                        self.init_key = _key(message["id"])
            if initialize:
                self.init_line = line
            try:
                if child is None:
                    raise OSError("no runtime")
                child.stdin.write(line)
                child.stdin.flush()
            except OSError:
                with self.lock:
                    lost = [k for k in (_key(m["id"]) for m in requests) if k in self.pending]
                    self.pending.difference_update(lost)
                for key in lost:
                    self._error(key)
        self._stop()
        return 0


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
    if command == "mcp":
        return _Relay(project, rest).run()
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
