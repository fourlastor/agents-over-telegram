"""The relay API: agents' tool calls go to the always-on collector over a local socket.

(relay.py is the older Codex desktop relay's notification store; it is unrelated.)

``aot serve`` runs the newest installed runtime and is the one process that
fetches from Telegram. Since the relay API it also answers agents' tool calls
(``messages_poll``, ``message_send`` and the rest): an agent's MCP server or
``aot tool`` command sends the call over a Unix socket in the state directory,
and the relay runs it with the same validation, scoping and envelope as a
local call (mcp_server.call_tool). So every agent's work runs on the relay's
code, whatever runtime its project points at.

Access needs all of:
- the socket directory (0700) and socket (0600) belong to this user;
- the connecting process runs as this user (SO_PEERCRED / LOCAL_PEERCRED);
- the request carries the client's secret, which picks the only inbox it
  can reach.

Each request names its protocol version. A relay refuses a node older than
MIN_PROTOCOL with ``node_too_old``; a node finding an older relay, or none,
runs the call itself as before. Once a request has been sent the node never
runs it a second time: a lost answer is reported, not retried, because a send
may already have gone out.
"""
from __future__ import annotations

import errno
import json
import os
import secrets
import socket
import socketserver
import stat
import struct
import sys
import threading
from pathlib import Path

from .credentials import private_directory

PROTOCOL = 1
MIN_PROTOCOL = 1
SOCKET_DIR = 'relay'
SOCKET_NAME = 'relay.sock'
MAX_REQUEST = 1024 * 1024
MAX_RESPONSE = 4 * 1024 * 1024
# A call can wait on a Telegram request (a send, an attachment download).
CALL_TIMEOUT = 90.0
CONNECT_TIMEOUT = 2.0
# The longest a waiting agent's request is held before it is answered anyway.
WAIT_LIMIT = 50.0
# Calls after which other agents may have something new (a peer delivery).
_CHANGING_TOOLS = frozenset({'message_send'})


class Unavailable(Exception):
    """No relay took the request; the caller may run it locally."""


# Unix socket paths are limited to about 104 bytes (macOS) or 108 (Linux).
MAX_SOCKET_PATH = 100


def socket_path(state):
    """The relay socket for a state directory.

    Normally ``<state>/relay/relay.sock``. A state directory whose path is too
    long for a Unix socket gets a short one in a private per-user runtime
    directory instead ($XDG_RUNTIME_DIR, or /tmp/aot-<uid>), named after the
    state directory's path, so the relay and its agents agree on it.
    """
    state = Path(state).absolute()
    path = state / SOCKET_DIR / SOCKET_NAME
    if len(os.fsencode(path)) <= MAX_SOCKET_PATH:
        return path
    import hashlib
    runtime = os.environ.get('XDG_RUNTIME_DIR')
    base = Path(runtime) if runtime and Path(runtime).is_absolute() else Path('/tmp') / f'aot-{os.getuid()}'
    digest = hashlib.sha256(os.fsencode(os.path.realpath(state))).hexdigest()[:16]
    return base / f'aot-relay-{digest}' / SOCKET_NAME


def peer_uid(connection):
    """The user id of the process on the other end of a Unix socket, or None when unknown."""
    if hasattr(socket, 'SO_PEERCRED'):
        size = struct.calcsize('3i')
        _pid, uid, _gid = struct.unpack('3i', connection.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, size))
        return uid
    if sys.platform == 'darwin':
        # SOL_LOCAL=0, LOCAL_PEERCRED=1: struct xucred {u_int version; uid_t uid; short ngroups; gid_t groups[16]}.
        data = connection.getsockopt(0, 1, struct.calcsize('IIh16I'))
        return struct.unpack_from('II', data)[1]
    return None


def _read_line(connection, limit):
    chunks, size = [], 0
    while True:
        chunk = connection.recv(65536)
        if not chunk:
            break
        chunks.append(chunk)
        size += len(chunk)
        if b'\n' in chunk:
            break
        if size > limit:
            raise ValueError('too_large')
    data = b''.join(chunks)
    line, _, _ = data.partition(b'\n')
    if len(line) > limit:
        raise ValueError('too_large')
    return line


def _send(connection, value):
    connection.sendall(json.dumps(value, ensure_ascii=False, separators=(',', ':')).encode('utf-8') + b'\n')


class Relay:
    """Answers tool calls on the state directory's socket from a background thread."""

    def __init__(self, core, state, log=None):
        self.core, self.state = core, Path(state)
        self.log = log or (lambda text: None)
        self.server = None
        # Waiting agents hold a request until the generation moves: after a
        # collection routed updates, or a call that may deliver to a peer. The
        # instance tells a waiter that the relay restarted.
        self.instance = secrets.token_hex(8)
        self.generation = 0
        self.closing = False
        self.changed = threading.Condition()

    def notify(self):
        """Wake every waiting agent; each then checks its own inbox."""
        with self.changed:
            self.generation += 1
            self.changed.notify_all()

    def _wait(self, instance, generation, timeout):
        with self.changed:
            if instance == self.instance and generation == self.generation:
                self.changed.wait_for(lambda: self.generation != generation or self.closing, timeout)
            return {'instance': self.instance, 'generation': self.generation}

    def answer(self, request):
        """One request's answer; never raises."""
        from .core import CoreError
        from .mcp_server import _error_body, call_tool
        if not isinstance(request, dict) or not isinstance(request.get('protocol'), int):
            return {'protocol': PROTOCOL, 'ok': False, 'error': 'bad_request'}
        if request['protocol'] < MIN_PROTOCOL:
            # Fixed text: the node's project points at a runtime this relay no longer serves.
            return {'protocol': PROTOCOL, 'ok': False, 'error': 'node_too_old', 'min_protocol': MIN_PROTOCOL}
        if request['protocol'] > PROTOCOL:
            return {'protocol': PROTOCOL, 'ok': False, 'error': 'relay_too_old'}
        secret, tool, arguments = request.get('secret'), request.get('tool'), request.get('arguments')
        if not isinstance(secret, str) or not (isinstance(tool, str) or request.get('op') == 'wait'):
            return {'protocol': PROTOCOL, 'ok': False, 'error': 'bad_request'}
        try:
            client_id = self.core.authenticate(secret)
        except CoreError:
            return {'protocol': PROTOCOL, 'ok': True, 'body': _error_body('authentication_failed')}
        if request.get('op') == 'wait':
            timeout = request.get('timeout')
            if isinstance(timeout, bool) or not isinstance(timeout, (int, float)) or not 0 <= timeout <= WAIT_LIMIT:
                return {'protocol': PROTOCOL, 'ok': False, 'error': 'bad_request'}
            return {'protocol': PROTOCOL, 'ok': True,
                    'wait': self._wait(request.get('instance'), request.get('generation'), timeout)}
        body = call_tool(self.core, client_id, tool, arguments)
        if tool in _CHANGING_TOOLS and body.get('ok'):
            self.notify()
        return {'protocol': PROTOCOL, 'ok': True, 'body': body}

    def handle(self, connection):
        try:
            connection.settimeout(CALL_TIMEOUT)
            if peer_uid(connection) not in (None, os.getuid()):
                _send(connection, {'protocol': PROTOCOL, 'ok': False, 'error': 'forbidden'})
                return
            try:
                request = json.loads(_read_line(connection, MAX_REQUEST))
            except (ValueError, UnicodeDecodeError):
                _send(connection, {'protocol': PROTOCOL, 'ok': False, 'error': 'bad_request'})
                return
            _send(connection, self.answer(request))
        except OSError:
            pass

    def start(self):
        """Listen on the socket; returns the server thread."""
        path = socket_path(self.state)
        if self.state.absolute() not in path.parents:
            # The short path's runtime directory must be ours too, not one
            # another user created first in /tmp.
            private_directory(path.parent.parent)
        private_directory(path.parent)
        try:
            info = path.lstat()
        except FileNotFoundError:
            pass
        else:
            # Only a socket of ours, left by a relay that stopped, is replaced.
            if not stat.S_ISSOCK(info.st_mode) or info.st_uid != os.getuid():
                raise OSError(errno.EEXIST, 'relay socket path is taken')
            probe = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            probe.settimeout(CONNECT_TIMEOUT)
            try:
                probe.connect(str(path))
            except OSError:
                path.unlink()
            else:
                raise OSError(errno.EADDRINUSE, 'another relay answers on this socket')
            finally:
                probe.close()
        relay = self

        class Handler(socketserver.BaseRequestHandler):
            def handle(self):
                relay.handle(self.request)

        class Server(socketserver.ThreadingMixIn, socketserver.UnixStreamServer):
            daemon_threads = True

        previous = os.umask(0o177)
        try:
            self.server = Server(str(path), Handler)
        finally:
            os.umask(previous)
        os.chmod(path, 0o600)
        thread = threading.Thread(target=self.server.serve_forever, name='aot-relay', daemon=True)
        thread.start()
        self.log(f'aot serve: answering agents on {path}')
        return thread

    def stop(self):
        with self.changed:
            self.closing = True
            self.changed.notify_all()
        if self.server is not None:
            self.server.shutdown()
            self.server.server_close()
            try:
                socket_path(self.state).unlink()
            except OSError:
                pass
            self.server = None


def _request(state, request, timeout):
    """Send one request; its answer, None when the answer was lost, Unavailable when nobody took it."""
    path = socket_path(state)
    try:
        info = path.parent.lstat()
        if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077:
            raise Unavailable('unsafe_relay_directory')
    except FileNotFoundError:
        raise Unavailable('no_relay') from None
    connection = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    try:
        connection.settimeout(CONNECT_TIMEOUT)
        try:
            connection.connect(str(path))
        except OSError:
            raise Unavailable('no_relay') from None
        if peer_uid(connection) not in (None, os.getuid()):
            raise Unavailable('foreign_relay')
        connection.settimeout(timeout)
        try:
            _send(connection, {'protocol': PROTOCOL, **request})
            answer = json.loads(_read_line(connection, MAX_RESPONSE))
        except (OSError, ValueError, UnicodeDecodeError):
            return None
    finally:
        connection.close()
    if not isinstance(answer, dict):
        return None
    if answer.get('error') == 'relay_too_old':
        raise Unavailable('relay_too_old')
    return answer


def call(state, secret, tool, arguments, timeout=CALL_TIMEOUT):
    """Run one tool call on the relay and return its body.

    Raises Unavailable when no relay took the request (no socket, nobody
    listening, a relay older than this node); the caller then runs it itself.
    A request that was sent is never run again: a lost answer comes back as
    relay_no_answer.
    """
    from .mcp_server import _error_body
    answer = _request(state, {'secret': secret, 'tool': tool, 'arguments': arguments}, timeout)
    if answer is None:
        return _error_body('relay_no_answer')
    if answer.get('ok') and isinstance(answer.get('body'), dict):
        return answer['body']
    if answer.get('error') == 'node_too_old':
        return _error_body('node_too_old')
    return _error_body('relay_refused')


class Waiter:
    """Blocks a watching agent until the relay has news, instead of polling the inbox.

    Calling it waits up to ``timeout`` seconds and returns True when the relay
    answered (with news or not), False when no relay is there; the watcher
    then collects and paces itself as before.
    """

    def __init__(self, state, secret):
        self.state, self.secret = Path(state), secret
        self.instance = self.generation = None

    def __call__(self, timeout):
        timeout = max(0.0, min(float(timeout), WAIT_LIMIT))
        try:
            answer = _request(self.state, {'op': 'wait', 'secret': self.secret, 'instance': self.instance,
                                           'generation': self.generation, 'timeout': timeout}, timeout + 10)
        except Unavailable:
            return False
        wait = answer.get('wait') if isinstance(answer, dict) else None
        if not isinstance(wait, dict):
            return False
        self.instance, self.generation = wait.get('instance'), wait.get('generation')
        return True
