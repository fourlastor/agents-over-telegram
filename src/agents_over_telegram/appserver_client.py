"""Bounded, local-only JSON-RPC client for an explicitly selected Codex backend.

This transport never discovers a backend, reconnects a broken session, retries a
mutation, logs server content, or approves requests received from the server.
"""
from __future__ import annotations

import ipaddress
import json
import logging
import math
import os
from pathlib import Path
import socket
import stat
import struct
import threading
import time
from urllib.parse import urlsplit
from uuid import UUID

from websockets.sync.client import connect, unix_connect

from . import __version__
from .credentials import CredentialError, read_private_json


_EVENT_METHODS = frozenset({
    "thread/started", "thread/status/changed", "turn/started", "turn/completed",
    "item/started", "item/completed", "item/commandExecution/requestApproval",
    "item/fileChange/requestApproval", "item/tool/requestUserInput", "item/tool/call",
    "item/permissions/requestApproval", "account/chatgptAuthTokens/refresh",
    "execCommandApproval", "applyPatchApproval", "error", "warning",
})


class AppServerError(Exception):
    """A secret-free error; ambiguous mutations require manual reconciliation."""

    def __init__(self, code, *, ambiguous=False, rpc_code=None):
        self.code = code
        self.ambiguous = ambiguous
        self.rpc_code = rpc_code
        super().__init__(code)


def validate_endpoint(endpoint, *, token_file=None):
    """Validate configuration without connecting or reading a credential."""
    if not isinstance(endpoint, str) or not endpoint or len(endpoint) > 4096:
        raise AppServerError("invalid_appserver_endpoint")
    try:
        parsed = urlsplit(endpoint)
        if parsed.scheme == "unix":
            if (parsed.netloc or not parsed.path.startswith("/") or parsed.query
                    or parsed.fragment or "%" in parsed.path or "\x00" in parsed.path
                    or token_file is not None):
                raise ValueError()
            if any(part in (".", "..") for part in parsed.path.split("/")):
                raise ValueError()
            return "unix", parsed.path
        if (parsed.scheme != "ws" or parsed.username is not None
                or parsed.password is not None or parsed.query or parsed.fragment
                or parsed.port is None or not 1 <= parsed.port <= 65535
                or token_file is None or not ipaddress.ip_address(parsed.hostname).is_loopback):
            raise ValueError()
        if any(ord(character) < 33 or ord(character) > 126 for character in endpoint):
            raise ValueError()
        return "ws", endpoint
    except (ValueError, TypeError):
        raise AppServerError("invalid_appserver_endpoint") from None


def _socket_parent(path):
    for ancestor in reversed(path.parents):
        info = ancestor.lstat()
        trusted_sticky = info.st_uid == 0 and info.st_mode & stat.S_ISVTX
        if (not stat.S_ISDIR(info.st_mode) or info.st_uid not in (0, os.getuid())
                or (info.st_mode & 0o022 and not trusted_sticky)):
            raise AppServerError("unsafe_appserver_socket")
    parent = path.parent.lstat()
    if parent.st_uid != os.getuid() or parent.st_mode & 0o077:
        raise AppServerError("unsafe_appserver_socket")
    return parent.st_dev, parent.st_ino


def _private_socket(path):
    """Validate a private socket or Codex's private, owned leaf socket alias.

    Codex may publish the configured endpoint as a symlink into its private
    short-path directory. Only a single absolute leaf alias is accepted. Its
    parent and the actual socket's parent must both pass the same protection
    checks; the resolved target is used for connect and identities are rechecked.
    """
    path = Path(path)
    try:
        parent_identity = _socket_parent(path)
        info = path.lstat()
        alias_identity = None
        if stat.S_ISLNK(info.st_mode):
            if info.st_uid != os.getuid():
                raise AppServerError("unsafe_appserver_socket")
            target = os.readlink(path)
            if not target.startswith("/") or any(part in (".", "..") for part in target.split("/")):
                raise AppServerError("unsafe_appserver_socket")
            alias_identity = (info.st_dev, info.st_ino, parent_identity, target)
            path = Path(target)
            parent_identity = _socket_parent(path)
            info = path.lstat()
        if (not stat.S_ISSOCK(info.st_mode) or info.st_uid != os.getuid()
                or info.st_mode & 0o077):
            raise AppServerError("unsafe_appserver_socket")
        return str(path), (info.st_dev, info.st_ino, parent_identity, alias_identity)
    except OSError:
        raise AppServerError("appserver_socket_unavailable") from None


def _auth_header(token_file):
    try:
        token = read_private_json(token_file).get("token")
    except CredentialError:
        raise AppServerError("appserver_credentials_unavailable") from None
    if (not isinstance(token, str) or not 16 <= len(token) <= 4096
            or any(ord(character) < 33 or ord(character) > 126 for character in token)):
        raise AppServerError("invalid_appserver_credentials")
    return {"Authorization": "Bearer " + token}


def _check_peer(raw):
    if hasattr(socket, "SO_PEERCRED"):
        credentials = raw.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, struct.calcsize("3i"))
        _, uid, _ = struct.unpack("3i", credentials)
        if uid != os.getuid():
            raise AppServerError("unsafe_appserver_peer")
    elif hasattr(raw, "getpeereid"):
        uid, _ = raw.getpeereid()
        if uid != os.getuid():
            raise AppServerError("unsafe_appserver_peer")


class AppServerConnection:
    """One serial connection. Call ``connect`` or use it as a context manager.

    Requests are synchronous, but notifications and reverse requests are handled
    while waiting for each response. They are bounded and never stored as logs.
    """

    def __init__(self, endpoint, *, token_file=None, timeout=10.0,
                 max_message_bytes=1024 * 1024, max_events=256):
        self.kind, self.address = validate_endpoint(endpoint, token_file=token_file)
        if (isinstance(timeout, bool) or not isinstance(timeout, (int, float))
                or not math.isfinite(timeout) or not 0 < timeout <= 300
                or type(max_message_bytes) is not int or not 256 <= max_message_bytes <= 16 * 1024 * 1024
                or type(max_events) is not int or not 1 <= max_events <= 4096):
            raise AppServerError("invalid_appserver_limits")
        self.token_file = token_file
        self.timeout = timeout
        self.max_message_bytes = max_message_bytes
        self.max_events = max_events
        self.notifications_received = 0
        self.server_requests_received = 0
        self._events = []
        self._socket = None
        self._used = False
        self._counter = 0
        self._lock = threading.Lock()

    def connect(self):
        if self._used:
            raise AppServerError("appserver_connection_already_used")
        self._used = True
        # A private logger also prevents global DEBUG settings exposing headers,
        # prompts, tool output, or server error descriptions through websockets.
        logger = logging.Logger("aot.appserver.private")
        logger.disabled = True
        options = dict(open_timeout=self.timeout, close_timeout=min(self.timeout, 1.0),
                       max_size=self.max_message_bytes, max_queue=16, proxy=None,
                       compression=None, logger=logger)
        raw = None
        try:
            if self.kind == "unix":
                identity = _private_socket(self.address)
                raw = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
                raw.settimeout(self.timeout)
                raw.connect(identity[0])
                _check_peer(raw)
                if _private_socket(self.address) != identity:
                    raise AppServerError("unsafe_appserver_socket")
                # websockets preserves the timeout of a supplied socket. Its
                # reader must remain alive while this persistent client is idle;
                # individual JSON-RPC receives have their own bounded timeout.
                raw.settimeout(None)
                self._socket = unix_connect(sock=raw, uri="ws://localhost/rpc", **options)
            else:
                self._socket = connect(self.address, additional_headers=_auth_header(self.token_file), **options)
            self.request("initialize", {
                "clientInfo": {"name": "agents_over_telegram_dispatcher", "version": __version__},
                "capabilities": {"experimentalApi": True},
            })
            self._socket.send(json.dumps({"method": "initialized"}, separators=(",", ":")))
            return self
        except AppServerError:
            self.close()
            if raw is not None:
                raw.close()
            raise
        except Exception:
            self.close()
            if raw is not None:
                raw.close()
            raise AppServerError("appserver_connect_failed") from None

    def __enter__(self):
        return self.connect()

    def __exit__(self, *_):
        self.close()

    def close(self):
        current, self._socket = self._socket, None
        if current is not None:
            try:
                current.close()
            except Exception:
                pass

    def request(self, method, params, *, mutating=False):
        if self._socket is None:
            raise AppServerError("appserver_not_connected")
        if not self._lock.acquire(blocking=False):
            raise AppServerError("appserver_concurrent_request")
        attempted = False
        timer = None
        timer_lock = threading.Lock()
        finished = False
        timed_out = False
        current = self._socket

        def expire():
            nonlocal timed_out
            with timer_lock:
                if finished:
                    return
                timed_out = True
                # WebSocket send() has no timeout argument. Shutdown also bounds
                # a stalled send without changing the persistent reader timeout.
                try:
                    current.socket.shutdown(socket.SHUT_RDWR)
                except OSError:
                    pass
        try:
            if (not isinstance(method, str) or not 1 <= len(method) <= 256
                    or not isinstance(params, dict) or type(mutating) is not bool):
                raise AppServerError("invalid_appserver_request")
            self._counter += 1
            request_id = f"aot-{self._counter}"
            try:
                outgoing = json.dumps({"id": request_id, "method": method, "params": params},
                                      separators=(",", ":"), allow_nan=False)
            except (TypeError, ValueError, RecursionError):
                raise AppServerError("invalid_appserver_request") from None
            if len(outgoing.encode("utf-8")) > self.max_message_bytes:
                raise AppServerError("appserver_request_too_large")
            deadline = time.monotonic() + self.timeout
            timer = threading.Timer(self.timeout, expire)
            timer.daemon = True
            timer.start()
            # A send exception may occur after some or all bytes reached the peer.
            attempted = True
            self._socket.send(outgoing)
            while True:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise TimeoutError()
                raw = self._socket.recv(timeout=remaining)
                message = self._parse(raw)
                if "method" in message:
                    self._handle_event(message)
                    continue
                if message.get("id") != request_id or type(message.get("id")) is not str:
                    raise AppServerError("appserver_response_id_mismatch")
                if ("result" in message) == ("error" in message):
                    raise AppServerError("invalid_appserver_response")
                if "error" in message:
                    error = message["error"]
                    if not isinstance(error, dict) or type(error.get("code")) is not int:
                        raise AppServerError("invalid_appserver_response")
                    # Error descriptions/data may contain secrets or chat content.
                    # Internal and application errors may follow a committed
                    # mutation. Only method/parameter rejection proves that it
                    # could not have run; -32600 is also used for runtime errors.
                    ambiguous = mutating and error["code"] not in (-32601, -32602)
                    raise AppServerError("appserver_rpc_error", ambiguous=ambiguous,
                                         rpc_code=error["code"])
                return message["result"]
        except AppServerError as exc:
            if attempted and exc.code != "appserver_rpc_error":
                self.close()
                raise AppServerError(exc.code, ambiguous=mutating) from None
            raise
        except TimeoutError:
            self.close()
            raise AppServerError("appserver_timeout", ambiguous=mutating and attempted) from None
        except Exception:
            self.close()
            code = "appserver_timeout" if timed_out else "appserver_transport_failed"
            raise AppServerError(code, ambiguous=mutating and attempted) from None
        finally:
            with timer_lock:
                finished = True
                if timer is not None:
                    timer.cancel()
            self._lock.release()

    def _handle_event(self, message):
        if (not isinstance(message["method"], str) or not 1 <= len(message["method"]) <= 256
                or "result" in message or "error" in message):
            raise AppServerError("invalid_appserver_response")
        if "id" not in message:
            self.notifications_received += 1
            # Busy turns can emit hundreds of deltas between RPC replies. They
            # are irrelevant to wake delivery and must not fill the control-event
            # buffer or make an otherwise accepted mutation look uncertain.
            if message["method"] not in _EVENT_METHODS:
                return
            kind = "notification"
        else:
            request_id = message["id"]
            if (type(request_id) not in (int, str)
                    or isinstance(request_id, str) and len(request_id) > 256):
                raise AppServerError("invalid_appserver_response")
            self.server_requests_received += 1
            kind = "server_request"
            # Requests are broadcast to the owning desktop too. Responding with
            # either approval OR an error would race the actual owner's answer.
            # Leave them unanswered and expose content-free attention metadata.
        event = {"kind": kind, "method": message["method"] if message["method"] in _EVENT_METHODS else "other"}
        params = message.get("params")
        if isinstance(params, dict):
            for field in ("threadId", "turnId"):
                value = params.get(field)
                if field == "turnId" and value is None and isinstance(params.get("turn"), dict):
                    value = params["turn"].get("id")
                if isinstance(value, str):
                    try:
                        event[field] = str(UUID(value))
                    except ValueError:
                        pass
        if len(self._events) >= self.max_events:
            raise AppServerError("appserver_event_limit")
        self._events.append(event)

    def _parse(self, raw):
        if not isinstance(raw, str) or len(raw.encode("utf-8")) > self.max_message_bytes:
            raise AppServerError("invalid_appserver_response")
        try:
            message = json.loads(raw)
        except (ValueError, RecursionError):
            raise AppServerError("invalid_appserver_response") from None
        if not isinstance(message, dict) or message.get("jsonrpc", "2.0") != "2.0":
            raise AppServerError("invalid_appserver_response")
        return message

    def drain(self, timeout=0):
        """Return bounded content-free events; never answer server requests.

        A nonzero timeout waits for the first new event only. Buffered events
        from earlier requests are returned immediately. Call regularly while a
        connection is idle; WebSocket receive buffering also has a fixed limit.
        """
        if (isinstance(timeout, bool) or not isinstance(timeout, (int, float))
                or not math.isfinite(timeout) or not 0 <= timeout <= self.timeout):
            raise AppServerError("invalid_appserver_limits")
        if self._socket is None:
            raise AppServerError("appserver_not_connected")
        if not self._lock.acquire(blocking=False):
            raise AppServerError("appserver_concurrent_request")
        try:
            remaining = 0 if self._events else timeout
            frames = 0
            while len(self._events) < self.max_events and frames < self.max_events:
                try:
                    raw = self._socket.recv(timeout=remaining)
                except TimeoutError:
                    break
                frames += 1
                remaining = 0
                message = self._parse(raw)
                if "method" not in message:
                    raise AppServerError("appserver_unexpected_response")
                self._handle_event(message)
            events, self._events = self._events, []
            return events
        except AppServerError:
            self.close()
            raise
        except Exception:
            self.close()
            raise AppServerError("appserver_transport_failed") from None
        finally:
            self._lock.release()
