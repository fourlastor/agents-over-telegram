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
import time
from pathlib import Path

from . import compat
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
# The inbox work of hooks and waiters (push.py) the relay runs for agents.
NODE_OPS = frozenset({'hook', 'claim', 'watch_prepare'})
# Calls after which other agents may have something new (a peer delivery).
_CHANGING_TOOLS = frozenset({'message_send'})
# A request line must arrive within this, however slowly its bytes trickle in.
REQUEST_DEADLINE = 10.0
# At most this many requests are handled at once; more get "busy" and the
# agents run those calls themselves.
MAX_ACTIVE = 32
# Waiting agents (one per session) have their own slots.
MAX_WAITS = 64
# Hooks and inbox claims run inside host hooks with short time limits.
NODE_TIMEOUT = 4.0
# On stop, requests already in flight get this long to finish.
DRAIN_SECONDS = 3.0
# The relay records the socket it listens on here, inside the state
# directory, so agents whose environment differs (another TMPDIR) find it.
SOCKET_RECORD = 'relay-socket.json'


class Unavailable(Exception):
    """No relay took the request; the caller may run it locally."""


# Unix socket paths are limited to about 104 bytes (macOS) or 108 (Linux).
MAX_SOCKET_PATH = 100


def socket_path(state):
    """The relay socket for a state directory.

    Normally ``<state>/relay/relay.sock``. A state directory whose path is too
    long for a Unix socket gets a short one in a private per-user directory
    instead, named after the state directory's path, so the relay and its
    agents agree on it: $XDG_RUNTIME_DIR (Linux), $TMPDIR (per-user on
    macOS), whichever exists, is ours and private and fits, else
    /tmp/aot-<uid>.
    """
    state = Path(state).absolute()
    path = state / SOCKET_DIR / SOCKET_NAME
    if len(os.fsencode(path)) <= MAX_SOCKET_PATH:
        return path
    import hashlib
    digest = hashlib.sha256(os.fsencode(os.path.realpath(state))).hexdigest()[:16]
    for base in [*_private_runtime_dirs(), _short_base()]:
        short = base / f'aot-relay-{digest}' / SOCKET_NAME
        if len(os.fsencode(short)) <= MAX_SOCKET_PATH:
            return short
    return short


def _private_runtime_dirs():
    """Per-user runtime directories from the environment that exist, are ours and nobody else's."""
    for name in ('XDG_RUNTIME_DIR', 'TMPDIR'):
        value = os.environ.get(name)
        if not value or not os.path.isabs(value):
            continue
        try:
            info = os.lstat(value)
        except OSError:
            continue
        if stat.S_ISDIR(info.st_mode) and info.st_uid == compat.getuid() and not info.st_mode & 0o077:
            yield Path(value)


def _short_base():
    return Path('/tmp') / f'aot-{compat.getuid()}'


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


def _read_line(connection, limit, deadline=None):
    chunks, size = [], 0
    while True:
        if deadline is not None:
            left = deadline - time.monotonic()
            if left <= 0:
                raise ValueError('too_slow')
            connection.settimeout(left)
        try:
            chunk = connection.recv(65536)
        except TimeoutError:
            if deadline is None:
                raise
            raise ValueError('too_slow') from None
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
        self.path = None
        self.active = threading.BoundedSemaphore(MAX_ACTIVE)
        self.waits = threading.BoundedSemaphore(MAX_WAITS)
        self.connections = threading.BoundedSemaphore(MAX_ACTIVE + MAX_WAITS)
        # When an agent last called or waited (time.monotonic), for a
        # collector started on demand that stops when idle.
        self.last_activity = None
        # Call entries for the activity log, written once per collector cycle.
        self._activity = []

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
        op = request.get('op')
        if not isinstance(secret, str) or not (isinstance(tool, str) or op == 'wait' or op in NODE_OPS):
            return {'protocol': PROTOCOL, 'ok': False, 'error': 'bad_request'}
        try:
            client_id = self.core.authenticate(secret)
        except CoreError:
            if op in NODE_OPS:
                return {'protocol': PROTOCOL, 'ok': True, 'error_code': 'authentication_failed'}
            return {'protocol': PROTOCOL, 'ok': True, 'body': _error_body('authentication_failed')}
        if op in NODE_OPS:
            return self._node_op(op, client_id, request)
        if request.get('op') == 'wait':
            timeout = request.get('timeout')
            if isinstance(timeout, bool) or not isinstance(timeout, (int, float)) or not 0 <= timeout <= WAIT_LIMIT:
                return {'protocol': PROTOCOL, 'ok': False, 'error': 'bad_request'}
            return {'protocol': PROTOCOL, 'ok': True,
                    'wait': self._wait(request.get('instance'), request.get('generation'), timeout)}
        from .mcp_server import _TOOL_MAP, _VALIDATORS
        if tool not in _TOOL_MAP or not isinstance(arguments, dict) or not _VALIDATORS[tool].is_valid(arguments):
            # A newer agent may know a tool or argument this relay does not.
            # Nothing ran, so it can run the call itself.
            return {'protocol': PROTOCOL, 'ok': False, 'error': 'not_handled'}
        body = call_tool(self.core, client_id, tool, arguments)
        if tool in _CHANGING_TOOLS and body.get('ok'):
            # A send can deliver to a peer agent; every waiter then checks its
            # own inbox, which is cheap and finds nothing for most of them.
            self.notify()
        return {'protocol': PROTOCOL, 'ok': True, 'body': body}

    def _record(self, request, answer, started):
        """Log a call's duration and outcome; waits are not logged, they block by design."""
        op = request.get('op') if isinstance(request, dict) else None
        name = op or (request.get('tool') if isinstance(request, dict) else None)
        body = answer.get('body') if isinstance(answer, dict) else None
        if not isinstance(answer, dict) or not answer.get('ok'):
            outcome = (answer or {}).get('error') or 'bad_request'
        elif answer.get('error_code'):
            outcome = answer['error_code']
        elif isinstance(body, dict) and not body.get('ok'):
            outcome = (body.get('error') or {}).get('code') or 'error'
        else:
            outcome = 'ok'
        entry = ('relay', {'call': name if isinstance(name, str) else 'unknown', 'outcome': outcome,
                           'ms': round((time.monotonic() - started) * 1000)}, self.core.clock())
        # Buffered: a write per call would add a transaction to every hook.
        # The collector writes them once per cycle (flush_activity).
        with self.changed:
            self._activity.append(entry)
            del self._activity[:-self.core.ACTIVITY_KEEP]

    def flush_activity(self):
        """Write the buffered call entries to the activity log in one transaction."""
        with self.changed:
            entries, self._activity = self._activity, []
        self.core.record_activities(entries)

    def _node_op(self, op, client_id, request):
        """The inbox work of hooks and waiters, run here so agents never open the database."""
        from . import push
        from .core import CoreError
        try:
            if op == 'hook':
                event, payload = request.get('event'), request.get('payload')
                if not isinstance(event, str) or not isinstance(payload, dict):
                    return {'protocol': PROTOCOL, 'ok': False, 'error': 'bad_request'}
                result = push.hook_response(self.core, client_id, event, payload)
            elif op == 'claim':
                result = push.claim_new(self.core, client_id, request.get('surface'))
            else:  # watch_prepare
                push.prepare_watch(self.core, client_id, request.get('announce_backlog') is not False)
                result = None
        except CoreError as error:
            return {'protocol': PROTOCOL, 'ok': True, 'error_code': error.code}
        return {'protocol': PROTOCOL, 'ok': True, 'result': result}

    def handle(self, connection):
        self.last_activity = time.monotonic()

        def busy():
            try:
                _send(connection, {'protocol': PROTOCOL, 'ok': False, 'error': 'busy'})
            except OSError:
                pass
        # A connection slot covers reading the request (bounded by
        # REQUEST_DEADLINE); then a call slot or, for a wait, a wait slot, so
        # waiting agents cannot use up the slots calls need.
        if not self.connections.acquire(blocking=False):
            busy()
            return
        try:
            # A peer whose user cannot be determined is refused, like another user.
            if peer_uid(connection) != compat.getuid():
                _send(connection, {'protocol': PROTOCOL, 'ok': False, 'error': 'forbidden'})
                return
            try:
                request = json.loads(_read_line(connection, MAX_REQUEST, time.monotonic() + REQUEST_DEADLINE))
            except (ValueError, UnicodeDecodeError, RecursionError):
                _send(connection, {'protocol': PROTOCOL, 'ok': False, 'error': 'bad_request'})
                return
            slot = self.waits if isinstance(request, dict) and request.get('op') == 'wait' else self.active
            if not slot.acquire(blocking=False):
                busy()
                return
            try:
                connection.settimeout(CALL_TIMEOUT)
                started = time.monotonic()
                try:
                    answer = self.answer(request)
                except Exception:
                    answer = {'protocol': PROTOCOL, 'ok': False, 'error': 'bad_request'}
                _send(connection, answer)
                if slot is self.active:
                    self._record(request, answer, started)
            finally:
                slot.release()
        except OSError:
            pass
        finally:
            self.connections.release()

    def start(self):
        """Listen on the socket; returns the server thread."""
        if not hasattr(socket, 'AF_UNIX'):
            # Python on Windows has no Unix sockets; agents run their calls themselves.
            raise OSError(errno.EAFNOSUPPORT, 'Unix sockets are not available on this platform')
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
            if not stat.S_ISSOCK(info.st_mode) or info.st_uid != compat.getuid():
                raise OSError(errno.EEXIST, 'relay socket path is taken')
            probe = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            probe.settimeout(CONNECT_TIMEOUT)
            try:
                probe.connect(str(path))
            except ConnectionRefusedError:
                # Nobody listens: a relay that stopped left it behind.
                path.unlink()
            except OSError:
                # A timeout can be a busy relay with a full backlog; keep its socket.
                raise OSError(errno.EADDRINUSE, 'the relay socket did not answer') from None
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
        self.path = path
        from .credentials import write_private_json
        record = self.state / SOCKET_RECORD
        write_private_json(record, {'version': 1, 'socket': str(path)}, overwrite=record.exists())
        thread = threading.Thread(target=self.server.serve_forever, name='aot-relay', daemon=True)
        thread.start()
        self.log(f'aot serve: answering agents on {path}')
        return thread

    def stop(self):
        with self.changed:
            self.closing = True
            self.changed.notify_all()
        if self.server is not None:
            # Stop accepting, then let the requests already in flight finish:
            # a claim cut off mid-way leaves its agent with a lost answer and a
            # "messages may be waiting" notice for nothing.
            self.server.shutdown()
            self._drain()
            self.server.server_close()
            for leftover in (self.path or socket_path(self.state), self.state / SOCKET_RECORD):
                try:
                    leftover.unlink()
                except OSError:
                    pass
            self.server = None
        self.flush_activity()

    def _drain(self):
        """Wait up to DRAIN_SECONDS for every request in flight to finish."""
        deadline, taken = time.monotonic() + DRAIN_SECONDS, 0
        while taken < MAX_ACTIVE + MAX_WAITS:
            left = deadline - time.monotonic()
            if left <= 0 or not self.connections.acquire(timeout=left):
                break
            taken += 1
        for _ in range(taken):
            self.connections.release()


def recorded_socket(state):
    """The socket the running relay recorded, or None."""
    from .credentials import CredentialError, read_private_json
    try:
        value = read_private_json(Path(state) / SOCKET_RECORD).get('socket')
    except (CredentialError, OSError):
        return None
    return Path(value) if isinstance(value, str) and os.path.isabs(value) else None



def _request(state, request, timeout):
    """Send one request; its answer, None when the answer was lost, Unavailable when nobody took it."""
    if not hasattr(socket, 'AF_UNIX'):
        raise Unavailable('no_unix_sockets')
    path = recorded_socket(state) or socket_path(state)
    try:
        info = path.parent.lstat()
        if not stat.S_ISDIR(info.st_mode) or info.st_uid != compat.getuid() or info.st_mode & 0o077:
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
        if peer_uid(connection) != compat.getuid():
            raise Unavailable('foreign_relay')
        connection.settimeout(timeout)
        try:
            _send(connection, {'protocol': PROTOCOL, **request})
        except OSError:
            # A busy relay answers and closes before reading the request, so
            # the send can fail; its answer may still be waiting to be read.
            pass
        try:
            answer = json.loads(_read_line(connection, MAX_RESPONSE))
        except (OSError, ValueError, UnicodeDecodeError):
            return None
    finally:
        connection.close()
    if not isinstance(answer, dict):
        return None
    if answer.get('error') in ('relay_too_old', 'not_handled', 'busy'):
        # Nothing ran on the relay; the agent runs the call itself.
        raise Unavailable(answer['error'])
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


class Lost(Exception):
    """The relay took the request but its answer never arrived.

    The work may have run there (a claim may have recorded announcements), so
    the agent must not run it a second time on its own database handle.
    """


class Node:
    """An agent's side of the relay: hook answers, inbox claims, waits and status.

    Every method raises Unavailable when no relay took the request, and the
    caller then does the work itself on its own database handle. A relay that
    predates an operation answers bad_request before running anything, which
    counts as unavailable too.
    """

    def __init__(self, state, secret):
        self.state, self.secret = Path(state), secret
        self.waiter = Waiter(state, secret)

    def _op(self, op, **fields):
        answer = _request(self.state, {'op': op, 'secret': self.secret, **fields}, NODE_TIMEOUT)
        if answer is None:
            raise Lost(op)
        if not isinstance(answer, dict) or not answer.get('ok'):
            raise Unavailable(answer.get('error') or 'no_answer')
        if answer.get('error_code'):
            from .core import CoreError
            raise CoreError(answer['error_code'])
        return answer.get('result')

    def hook(self, event, payload):
        return self._op('hook', event=event, payload=payload)

    def claim(self, surface):
        return self._op('claim', surface=surface)

    def prepare_watch(self, announce_backlog):
        self._op('watch_prepare', announce_backlog=announce_backlog)

    def wait(self, timeout):
        return self.waiter(timeout)

    def status(self):
        body = call(self.state, self.secret, 'connection_status', {}, timeout=NODE_TIMEOUT * 2)
        if not body.get('ok'):
            from .core import CoreError
            raise CoreError((body.get('error') or {}).get('code') or 'relay_refused')
        return body['result']


class LazyCore:
    """A database handle opened on first use, for agents that use the relay.

    Agents keep a local path for when no relay answers; building a Core opens
    and migrates the database, so it waits until that path is actually taken.
    """

    def __init__(self, factory):
        self._factory, self._core = factory, None

    @property
    def opened(self):
        return self._core is not None

    def __getattr__(self, name):
        if self._core is None:
            self._core = self._factory()
        return getattr(self._core, name)
