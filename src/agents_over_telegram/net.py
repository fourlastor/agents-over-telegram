"""Outgoing HTTPS connections that race address families (RFC 8305, "Happy Eyeballs").

urllib connects through socket.create_connection, which tries one address at
a time and gives each the whole request timeout. On a network where IPv6
connects sometimes stall, every stalled connect costs the full timeout before
IPv4 is tried. Here the next address starts after a short delay, or as soon
as an attempt fails, and the first connection to succeed wins.
"""
from __future__ import annotations

import errno
import http.client
import os
import selectors
import socket
import time
import urllib.request

# How long an attempt runs alone before the next address joins the race.
ATTEMPT_DELAY = 0.25

_IN_PROGRESS = {errno.EINPROGRESS, errno.EWOULDBLOCK, errno.EALREADY, 10035}  # 10035: WSAEWOULDBLOCK


def _interleave(infos):
    """Alternate address families, starting with the resolver's first choice."""
    if not infos:
        return []
    first = infos[0][0]
    preferred = [info for info in infos if info[0] == first]
    others = [info for info in infos if info[0] != first]
    ordered = []
    while preferred or others:
        if preferred:
            ordered.append(preferred.pop(0))
        if others:
            ordered.append(others.pop(0))
    return ordered


def racing_connection(address, timeout=socket._GLOBAL_DEFAULT_TIMEOUT, source_address=None, *,
                      resolve=socket.getaddrinfo, delay=None):
    """A connected socket like socket.create_connection, racing the resolved addresses."""
    host, port = address[:2]
    delay = ATTEMPT_DELAY if delay is None else delay
    limited = timeout is not None and timeout is not socket._GLOBAL_DEFAULT_TIMEOUT
    deadline = time.monotonic() + timeout if limited else None
    queue = _interleave(resolve(host, port, 0, socket.SOCK_STREAM))
    if not queue:
        raise OSError('getaddrinfo returned an empty list')
    selector = selectors.DefaultSelector()
    pending = set()
    error = None
    winner = None
    next_start = time.monotonic()
    try:
        while winner is None and (queue or pending):
            now = time.monotonic()
            if deadline is not None and now >= deadline:
                break
            if queue and (not pending or now >= next_start):
                family, kind, proto, _, sockaddr = queue.pop(0)
                try:
                    sock = socket.socket(family, kind, proto)
                except OSError as failure:
                    error = failure
                    continue
                try:
                    sock.setblocking(False)
                    if source_address:
                        sock.bind(source_address)
                    code = sock.connect_ex(sockaddr)
                except OSError as failure:
                    sock.close()
                    error = failure
                    continue
                if code == 0:
                    winner = sock
                    break
                if code not in _IN_PROGRESS:
                    sock.close()
                    error = OSError(code, os.strerror(code))
                    continue
                selector.register(sock, selectors.EVENT_WRITE)
                pending.add(sock)
                next_start = now + delay
                continue
            limits = []
            if queue:
                limits.append(next_start - now)
            if deadline is not None:
                limits.append(deadline - now)
            for key, _ in selector.select(max(0, min(limits)) if limits else None):
                sock = key.fileobj
                selector.unregister(sock)
                pending.discard(sock)
                code = sock.getsockopt(socket.SOL_SOCKET, socket.SO_ERROR)
                if code == 0 and winner is None:
                    winner = sock
                    continue
                sock.close()
                if code:
                    error = OSError(code, os.strerror(code))
                    # A failed attempt hands its turn to the next address at once.
                    next_start = time.monotonic()
    finally:
        for sock in pending:
            if sock is not winner:
                sock.close()
        selector.close()
    if winner is None:
        if deadline is not None and time.monotonic() >= deadline:
            raise TimeoutError('timed out')
        raise error or OSError('connect failed')
    # Like socket.create_connection: the given timeout, else the global default.
    winner.settimeout(timeout if timeout is not socket._GLOBAL_DEFAULT_TIMEOUT else socket.getdefaulttimeout())
    return winner


class _RacingHTTPSConnection(http.client.HTTPSConnection):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._create_connection = racing_connection


class RacingHTTPSHandler(urllib.request.HTTPSHandler):
    """Replaces urllib's HTTPS handler; everything but the connect is unchanged."""

    def do_open(self, http_class, req, **kwargs):
        return super().do_open(_RacingHTTPSConnection, req, **kwargs)
