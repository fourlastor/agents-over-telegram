"""Preserve desktop stdio while sharing its app-server over a private Unix socket.

This executable is an opt-in CODEX_CLI_PATH replacement. It requires an explicit
AOT_CODEX_REAL_BINARY, and normal app-server launches additionally require an
AOT_CODEX_SOCKET whose parent is an existing private directory. Other CLI calls
are delegated verbatim. The bundled ``app-server proxy`` isn't suitable here:
it forwards raw bytes while ``--listen unix://...`` expects WebSocket framing.
"""
from __future__ import annotations

import fcntl
import json
import logging
import os
from pathlib import Path
import signal
import socket
import stat
import struct
import subprocess
import sys
import threading
import time
from collections.abc import Mapping, Sequence

from websockets.exceptions import ConnectionClosed
from websockets.sync.client import unix_connect


MAX_MESSAGE_BYTES = 64 * 1024 * 1024
START_TIMEOUT_SECONDS = 30.0
STOP_TIMEOUT_SECONDS = 5.0
MAX_DISPATCH_LOG_BYTES = 1024 * 1024
_WEBSOCKET_LOGGER = logging.Logger("aot.desktop_bridge.websocket", level=logging.CRITICAL + 1)
_WEBSOCKET_LOGGER.disabled = True
_WEBSOCKET_LOGGER.propagate = False


class BridgeError(ValueError):
    """A diagnostic which doesn't expose CLI arguments or environment values."""


def handshake_id(value) -> tuple[str, str | int] | None:
    """Match bounded JSON-RPC IDs without treating bool as an integer."""
    if type(value) is str and len(value) <= 256:
        return "string", value
    if type(value) is int and -(1 << 63) <= value < (1 << 63):
        return "integer", value
    return None


def is_stdio_app_server(args: Sequence[str]) -> bool:
    """Recognize a plain launch, leaving explicit transports and tools alone."""
    value_options = {
        "-c", "--config", "--enable", "--disable", "--code-mode-host",
        "--ws-auth", "--ws-token-file", "--ws-token-sha256",
        "--ws-shared-secret-file", "--ws-issuer", "--ws-audience",
        "--ws-max-clock-skew-seconds",
    }
    if any(arg in {"-h", "--help", "-V", "--version", "--stdio", "--listen"}
           or arg.startswith("--listen=") for arg in args):
        return False
    command_seen = False
    skip = False
    for arg in args:
        if skip:
            skip = False
            continue
        if arg in value_options:
            skip = True
        elif arg.startswith("-"):
            continue
        elif not command_seen and arg == "app-server":
            command_seen = True
        else:
            # Another CLI command or an app-server tooling subcommand.
            return False
    return command_seen and not skip


def real_binary(env: Mapping[str, str]) -> str:
    value = env.get("AOT_CODEX_REAL_BINARY", "")
    if not value or "\x00" in value or not Path(value).is_absolute():
        raise BridgeError("Set AOT_CODEX_REAL_BINARY to the absolute bundled Codex executable.")
    try:
        binary = Path(value).resolve(strict=True)
        current = Path(sys.argv[0]).resolve()
        if not binary.is_file() or not os.access(binary, os.X_OK) or binary == current:
            raise OSError
    except (OSError, RuntimeError):
        raise BridgeError("The selected Codex executable is unavailable or points back to this bridge.") from None
    return str(binary)


def validate_socket_path(value: str) -> Path:
    """Check every component before opening anything in the private directory."""
    path = Path(value)
    if (not value or "\x00" in value or not path.is_absolute()
            or ".." in path.parts or len(os.fsencode(value)) > 107):
        raise BridgeError("Set AOT_CODEX_SOCKET to an absolute Unix socket path of at most 107 bytes.")
    parent = path.parent
    try:
        for directory in [*reversed(parent.parents), parent]:
            info = directory.lstat()
            if not stat.S_ISDIR(info.st_mode) or info.st_uid not in {0, os.getuid()}:
                raise BridgeError("The socket path must use owned directories without symlinks.")
            writable = stat.S_IMODE(info.st_mode) & 0o022
            trusted_sticky = info.st_uid == 0 and bool(info.st_mode & stat.S_ISVTX)
            if writable and not trusted_sticky:
                raise BridgeError("The socket path has a directory writable by another user.")
        info = parent.lstat()
        if info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) != 0o700:
            raise BridgeError("The socket parent must be an existing user-owned directory with mode 0700.")
    except OSError:
        raise BridgeError("The socket parent must be an existing user-owned directory with mode 0700.") from None
    return path


def acquire_lock(path: Path) -> int:
    lock_path = path.with_name(path.name + ".lock")
    try:
        descriptor = os.open(lock_path, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600)
    except OSError:
        raise BridgeError("Could not safely open the shared backend lock.") from None
    try:
        info = os.fstat(descriptor)
        if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid()
                or stat.S_IMODE(info.st_mode) != 0o600 or info.st_nlink != 1):
            raise BridgeError("The shared backend lock must be a private user-owned regular file.")
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise BridgeError("A desktop bridge already owns this backend endpoint.") from None
        if os.path.lexists(path):
            raise BridgeError("The shared backend endpoint already exists; refusing to replace it.")
        return descriptor
    except BaseException:
        os.close(descriptor)
        raise


def socket_identity(path: Path) -> tuple[tuple[int, int], tuple[int, int]]:
    """Codex publishes a custom endpoint as an owned link to its private socket."""
    info = path.lstat()
    if info.st_uid != os.getuid():
        raise BridgeError("The backend endpoint is not owned by the current user.")
    entry = (info.st_dev, info.st_ino)
    target = path
    if stat.S_ISLNK(info.st_mode):
        target = validate_socket_path(os.readlink(path))
        info = target.lstat()
    if (not stat.S_ISSOCK(info.st_mode) or info.st_uid != os.getuid()
            or stat.S_IMODE(info.st_mode) & 0o077):
        raise BridgeError("The backend did not create a private user-owned Unix socket.")
    return entry, (info.st_dev, info.st_ino)


def connect_backend(path: Path, process: subprocess.Popen, identity):
    transport = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    transport.settimeout(5)
    try:
        transport.connect(str(path))
        if hasattr(socket, "SO_PEERCRED"):
            peer_pid, peer_uid, _ = struct.unpack(
                "3i", transport.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, struct.calcsize("3i"))
            )
            if peer_uid != os.getuid() or peer_pid != process.pid:
                raise BridgeError("The Unix socket peer is not the backend started by this bridge.")
        if socket_identity(path) != identity:
            raise BridgeError("The backend endpoint changed during connection.")
        # websockets retains a supplied socket's timeout for its reader thread.
        # Only connect is bounded; an idle desktop must remain connected.
        transport.settimeout(None)
        connection = unix_connect(
            sock=transport, open_timeout=5, close_timeout=1,
            max_size=MAX_MESSAGE_BYTES, proxy=None, logger=_WEBSOCKET_LOGGER,
        )
        if socket_identity(path) != identity:
            connection.close()
            raise BridgeError("The backend endpoint changed during connection.")
        return connection
    except BaseException:
        transport.close()
        raise


def stop_backend(process: subprocess.Popen) -> None:
    """Signal only the process group that this wrapper created."""
    def send(signum):
        try:
            os.killpg(process.pid, signum)
        except ProcessLookupError:
            pass
        except PermissionError:
            # macOS may return EPERM after the leader has exited and the
            # group is empty. Never ignore it for a live or uninspectable group.
            if sys.platform != 'darwin' or process.poll() is None:
                raise
            from .macos_process import MacOSProcesses
            if not MacOSProcesses().group_is_empty(process.pid):
                raise
    send(signal.SIGTERM)
    try:
        process.wait(timeout=STOP_TIMEOUT_SECONDS)
    except subprocess.TimeoutExpired:
        send(signal.SIGKILL)
        process.wait(timeout=STOP_TIMEOUT_SECONDS)
    # The child may have exited before descendants which inherited its pipes.
    send(signal.SIGKILL)


def dispatcher_command(env: Mapping[str, str], path: Path) -> list[str] | None:
    """Start a dispatcher only when an operator explicitly selected its state."""
    state = env.get("AOT_DISPATCH_STATE", "")
    if not state:
        if env.get("AOT_DISPATCH_COLLECT") or env.get("AOT_DISPATCH_FAKE"):
            raise BridgeError("Dispatcher options require an explicit AOT_DISPATCH_STATE.")
        return None
    if "\x00" in state or not Path(state).is_absolute() or not Path(state).is_dir():
        raise BridgeError("AOT_DISPATCH_STATE must identify an existing absolute state directory.")
    for key in ("AOT_DISPATCH_COLLECT", "AOT_DISPATCH_FAKE"):
        if env.get(key, "0") not in {"0", "1"}:
            raise BridgeError("Dispatcher switches must be explicitly 0 or 1.")
    result = [sys.executable, "-m", "agents_over_telegram.cli", "--state-dir", state]
    if env.get("AOT_DISPATCH_FAKE", "0") == "1":
        result.append("--fake")
    result.extend(["relay-dispatch", "--endpoint", "unix://" + str(path), "--duration", "0"])
    if env.get("AOT_DISPATCH_COLLECT", "0") == "1":
        result.append("--collect")
    return result


def open_dispatch_log(path: Path) -> int:
    try:
        descriptor = os.open(path, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600)
    except OSError:
        raise BridgeError("Could not safely open the dispatcher metadata log.") from None
    info = os.fstat(descriptor)
    if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid()
            or stat.S_IMODE(info.st_mode) != 0o600 or info.st_nlink != 1):
        os.close(descriptor)
        raise BridgeError("The dispatcher log must be a private user-owned regular file.")
    if info.st_size > MAX_DISPATCH_LOG_BYTES:
        os.ftruncate(descriptor, 0)
    os.lseek(descriptor, 0, os.SEEK_END)
    return descriptor


def append_dispatch_log(descriptor: int, chunk: bytes) -> None:
    if os.lseek(descriptor, 0, os.SEEK_CUR) + len(chunk) > MAX_DISPATCH_LOG_BYTES:
        os.ftruncate(descriptor, 0)
        os.lseek(descriptor, 0, os.SEEK_SET)
    pending = memoryview(chunk[-MAX_DISPATCH_LOG_BYTES:])
    while pending:
        pending = pending[os.write(descriptor, pending):]


def _owned_child_command(command: Sequence[str]) -> list[str]:
    # Avoid preexec_fn after the bridge has threads: initialize parent-death
    # handling in a fresh interpreter, then exec the intended child verbatim.
    return [sys.executable, "-m", __spec__.name, "--owned-child", str(os.getpid()), *command]


def _exec_owned_child(args: Sequence[str]) -> int:
    if len(args) < 2 or not args[0].isdigit() or int(args[0]) != os.getppid():
        return 126
    parent_pid = int(args[0])
    if sys.platform == "linux":
        import ctypes
        libc = ctypes.CDLL(None, use_errno=True)
        if libc.prctl(1, signal.SIGTERM, 0, 0, 0) != 0:
            return 126
        if os.getppid() != parent_pid:
            return 126
    os.execv(args[1], list(args[1:]))
    return 126


def run_bridge(binary: str, args: Sequence[str], env: Mapping[str, str]) -> int:
    path = validate_socket_path(env.get("AOT_CODEX_SOCKET", ""))
    dispatch_args = dispatcher_command(env, path)
    lock = acquire_lock(path)
    process = None
    dispatcher = None
    log_fd = None
    log_thread = None
    connection = None
    identity = None
    done = threading.Event()
    initialized = threading.Event()
    handshake_lock = threading.Lock()
    initialize_request_id = None
    dispatch_log_failed = threading.Event()
    dispatch_disabled = False
    failures: list[str] = []
    caught_signal: list[int] = []
    handlers = {}

    def on_signal(number, _frame):
        caught_signal.append(number)
        done.set()

    try:
        for number in (signal.SIGINT, signal.SIGTERM, signal.SIGHUP):
            handlers[number] = signal.signal(number, on_signal)
        # Preserve the desktop's cwd, full configuration arguments, app-tools
        # environment and normal stderr. Only the transport is changed.
        process = subprocess.Popen(
            _owned_child_command([binary, *args, "--listen", "unix://" + str(path)]),
            stdin=subprocess.DEVNULL, stdout=sys.stderr, stderr=sys.stderr,
            env=dict(env), start_new_session=True,
        )
        deadline = time.monotonic() + START_TIMEOUT_SECONDS
        while not done.is_set():
            if process.poll() is not None:
                raise BridgeError("The shared Codex backend exited before accepting the desktop connection.")
            try:
                info = path.lstat()
            except FileNotFoundError:
                info = None
            if info is not None:
                try:
                    identity = socket_identity(path)
                    connection = connect_backend(path, process, identity)
                    break
                except (ConnectionRefusedError, FileNotFoundError):
                    pass
                except Exception:
                    raise BridgeError("Could not establish the desktop WebSocket connection.") from None
            if time.monotonic() >= deadline:
                raise BridgeError("Timed out waiting for the shared Codex backend.")
            done.wait(0.025)
        if done.is_set():
            return 128 + caught_signal[0] if caught_signal else 0

        def from_desktop():
            nonlocal initialize_request_id
            try:
                pending = bytearray()
                while not done.is_set():
                    chunk = os.read(sys.stdin.fileno(), 65536)
                    if not chunk:
                        if pending:
                            connection.send(bytes(pending).decode("utf-8"))
                        break
                    pending.extend(chunk)
                    while b"\n" in pending:
                        line, _, remainder = pending.partition(b"\n")
                        pending = bytearray(remainder)
                        line = line.rstrip(b"\r")
                        if len(line) > MAX_MESSAGE_BYTES:
                            raise BridgeError("A desktop protocol message exceeded the bridge size limit.")
                        if line:
                            text = bytes(line).decode("utf-8")
                            initialized_notification = False
                            if dispatch_args is not None and not initialized.is_set():
                                try:
                                    value = json.loads(text)
                                    if isinstance(value, dict):
                                        if value.get("method") == "initialize":
                                            # Register before send: the backend can answer
                                            # immediately on the other bridge thread.
                                            with handshake_lock:
                                                initialize_request_id = handshake_id(value.get("id"))
                                        elif value.get("method") == "initialized" and "id" not in value:
                                            initialized_notification = True
                                except (ValueError, TypeError):
                                    pass  # Leave protocol validation to the actual backend.
                            connection.send(text)
                            if initialized_notification:
                                initialized.set()
                    if len(pending) > MAX_MESSAGE_BYTES:
                        raise BridgeError("A desktop protocol message exceeded the bridge size limit.")
            except (ConnectionClosed, BrokenPipeError, OSError):
                if not done.is_set():
                    failures.append("The desktop input connection closed unexpectedly.")
            except (UnicodeError, BridgeError):
                failures.append("The desktop sent an invalid or oversized protocol message.")
            finally:
                done.set()

        def to_desktop():
            try:
                while not done.is_set():
                    message = connection.recv()
                    if not isinstance(message, str) or "\n" in message or "\r" in message:
                        raise BridgeError("The backend sent an invalid stdio protocol message.")
                    pending = memoryview(message.encode("utf-8") + b"\n")
                    while pending:
                        written = os.write(sys.stdout.fileno(), pending)
                        pending = pending[written:]
                    if dispatch_args is not None and not initialized.is_set():
                        try:
                            value = json.loads(message)
                            if (isinstance(value, dict) and "method" not in value
                                    and "result" in value and "error" not in value):
                                with handshake_lock:
                                    ready = (initialize_request_id is not None
                                             and handshake_id(value.get("id")) == initialize_request_id)
                                # The desktop's main connection omits `initialized`.
                                # Forward its successful response first so later
                                # notifications remain ordered after initialization.
                                if ready:
                                    initialized.set()
                        except (ValueError, TypeError):
                            pass
            except (ConnectionClosed, BrokenPipeError, OSError):
                if not done.is_set():
                    failures.append("The desktop backend connection closed unexpectedly.")
            except (UnicodeError, BridgeError):
                failures.append("The backend sent an invalid stdio protocol message.")
            finally:
                done.set()

        # A blocked desktop pipe must not prevent signal handling or cleanup.
        threading.Thread(target=from_desktop, name="aot-desktop-input", daemon=True).start()
        threading.Thread(target=to_desktop, name="aot-desktop-output", daemon=True).start()

        def disable_dispatcher(message: str):
            nonlocal dispatcher, dispatch_disabled
            dispatch_disabled = True
            if dispatcher is not None:
                stop_backend(dispatcher)
                dispatcher = None
            if log_thread is not None:
                log_thread.join(timeout=2)
            print(message, file=sys.stderr, flush=True)
            if log_fd is not None and not dispatch_log_failed.is_set() and (log_thread is None or not log_thread.is_alive()):
                try:
                    append_dispatch_log(log_fd, (json.dumps({"type": "dispatcher_error", "error": message}) + "\n").encode())
                except OSError:
                    pass

        while not done.wait(0.05):
            if dispatch_args is not None and initialized.is_set() and dispatcher is None and not dispatch_disabled:
                try:
                    log_fd = open_dispatch_log(path.parent / "dispatcher.jsonl")
                    dispatcher = subprocess.Popen(
                        _owned_child_command(dispatch_args), stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                        stderr=subprocess.STDOUT, env=dict(env), start_new_session=True, cwd=path.parent,
                    )
                except (BridgeError, OSError):
                    disable_dispatcher("The Telegram dispatcher could not start; desktop chats remain available.")
                    continue

                dispatch_stdout = dispatcher.stdout

                def copy_dispatch_log():
                    try:
                        pending = bytearray()
                        dropping = False
                        while True:
                            chunk = os.read(dispatch_stdout.fileno(), 65536)
                            if not chunk:
                                if pending and not dropping:
                                    append_dispatch_log(log_fd, bytes(pending) + b"\n")
                                break
                            pending.extend(chunk)
                            while b"\n" in pending:
                                line, _, remainder = pending.partition(b"\n")
                                pending = bytearray(remainder)
                                if dropping or len(line) > 65536:
                                    append_dispatch_log(log_fd, b'{"type":"dispatcher_log_line_omitted"}\n')
                                else:
                                    append_dispatch_log(log_fd, bytes(line) + b"\n")
                                dropping = False
                            if len(pending) > 65536:
                                pending.clear()
                                dropping = True
                    except OSError:
                        if not done.is_set():
                            dispatch_log_failed.set()

                log_thread = threading.Thread(target=copy_dispatch_log, name="aot-dispatch-log", daemon=True)
                log_thread.start()
            if not dispatch_disabled and dispatch_log_failed.is_set():
                disable_dispatcher("The Telegram dispatcher log failed; the dispatcher stopped and desktop chats remain available.")
            elif dispatcher is not None and dispatcher.poll() is not None:
                disable_dispatcher("The Telegram dispatcher exited; desktop chats remain available. Restart the desktop launcher to reconnect Telegram.")
            if process.poll() is not None:
                failures.append("The shared Codex backend exited.")
                done.set()
        if caught_signal:
            return 128 + caught_signal[0]
        if failures:
            raise BridgeError(failures[0])
        return 0
    finally:
        done.set()
        try:
            if dispatcher is not None:
                stop_backend(dispatcher)
            if log_thread is not None:
                log_thread.join(timeout=2)
            if log_fd is not None and (log_thread is None or not log_thread.is_alive()):
                os.close(log_fd)
            if connection is not None:
                try:
                    connection.close()
                except Exception:
                    pass
        finally:
            try:
                if process is not None:
                    stop_backend(process)
                if identity is not None:
                    try:
                        info = path.lstat()
                        if (info.st_dev, info.st_ino) == identity[0] and (stat.S_ISSOCK(info.st_mode) or stat.S_ISLNK(info.st_mode)):
                            path.unlink()
                    except FileNotFoundError:
                        pass
            finally:
                # Keep the lock inode stable; unlinking it permits a competing lock.
                os.close(lock)
                for number, previous in handlers.items():
                    signal.signal(number, previous)


def main() -> int:
    try:
        if sys.argv[1:2] == ["--owned-child"]:
            return _exec_owned_child(sys.argv[2:])
        binary = real_binary(os.environ)
        if not is_stdio_app_server(sys.argv[1:]):
            os.execv(binary, [binary, *sys.argv[1:]])
            raise AssertionError("execv returned")
        return run_bridge(binary, sys.argv[1:], os.environ)
    except BridgeError as exc:
        print(str(exc), file=sys.stderr)
    except OSError:
        print("The selected Codex process or private socket could not be opened.", file=sys.stderr)
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
