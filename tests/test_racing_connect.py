"""Outgoing connections race their addresses instead of waiting out a stalled one."""
import socket
import time
import urllib.request

import pytest

from agents_over_telegram import net
from agents_over_telegram.judge import DecisionJudge
from agents_over_telegram.transport import TelegramTransport

# TEST-NET-1 (RFC 5737) is never routed to a host: a connect there stalls or fails.
NOWHERE = '192.0.2.1'


@pytest.fixture
def listener():
    server = socket.socket()
    server.bind(('127.0.0.1', 0))
    server.listen(8)
    yield server.getsockname()[1]
    server.close()


def closed_port():
    probe = socket.socket()
    probe.bind(('127.0.0.1', 0))
    port = probe.getsockname()[1]
    probe.close()
    return port


def resolving(*addresses):
    def resolve(host, port, family, kind):
        return [(socket.AF_INET, socket.SOCK_STREAM, socket.IPPROTO_TCP, '', address) for address in addresses]
    return resolve


def test_a_stalled_first_address_does_not_cost_the_timeout(listener):
    started = time.monotonic()
    sock = net.racing_connection(('api.example', listener), 10,
                                 resolve=resolving((NOWHERE, listener), ('127.0.0.1', listener)))
    try:
        assert sock.getpeername() == ('127.0.0.1', listener)
        assert time.monotonic() - started < 3
    finally:
        sock.close()


def test_a_refused_address_hands_over_at_once(listener):
    started = time.monotonic()
    sock = net.racing_connection(('api.example', listener), 10, delay=5,
                                 resolve=resolving(('127.0.0.1', closed_port()), ('127.0.0.1', listener)))
    try:
        assert sock.getpeername() == ('127.0.0.1', listener)
        assert time.monotonic() - started < 3
    finally:
        sock.close()


def test_the_winner_is_blocking_with_the_request_timeout(listener):
    sock = net.racing_connection(('api.example', listener), 7, resolve=resolving(('127.0.0.1', listener)))
    try:
        assert sock.gettimeout() == 7
    finally:
        sock.close()


def test_nothing_answering_raises_within_the_timeout():
    started = time.monotonic()
    with pytest.raises(OSError):
        net.racing_connection(('api.example', 443), 0.5, resolve=resolving((NOWHERE, 443)))
    assert time.monotonic() - started < 3


def test_every_address_refusing_raises_the_failure():
    with pytest.raises(OSError) as raised:
        net.racing_connection(('api.example', 1), 5, resolve=resolving(('127.0.0.1', closed_port())))
    assert not isinstance(raised.value, TimeoutError)
    assert raised.value.strerror and raised.value.strerror != 'connect failed'


def test_without_a_timeout_the_winner_keeps_the_global_default(listener, monkeypatch):
    monkeypatch.setattr(socket, 'getdefaulttimeout', lambda: 3.0)
    sock = net.racing_connection(('api.example', listener), resolve=resolving(('127.0.0.1', listener)))
    try:
        assert sock.gettimeout() == 3.0
    finally:
        sock.close()


def test_families_alternate_from_the_resolvers_first_choice():
    six = [(socket.AF_INET6, 1, 6, '', (f'::{n}', 443, 0, 0)) for n in (1, 2)]
    four = [(socket.AF_INET, 1, 6, '', (f'10.0.0.{n}', 443)) for n in (1, 2)]
    ordered = net._interleave(six + four)
    assert [info[0] for info in ordered] == [socket.AF_INET6, socket.AF_INET, socket.AF_INET6, socket.AF_INET]
    assert [info[4][0] for info in ordered] == ['::1', '10.0.0.1', '::2', '10.0.0.2']


def https_handlers(opener):
    return [handler for handler in opener.handlers if isinstance(handler, urllib.request.HTTPSHandler)]


@pytest.mark.parametrize('opener', [
    lambda: TelegramTransport('987654:FAKE_SECRET_MUST_NEVER_ESCAPE_123456789')._opener,
    lambda: DecisionJudge('https://judge.example/v1', 'model')._opener,
], ids=['telegram', 'judge'])
def test_clients_connect_by_racing(opener):
    [handler] = https_handlers(opener())
    assert isinstance(handler, net.RacingHTTPSHandler)
    connection = net._RacingHTTPSConnection('api.example', 443)
    assert connection._create_connection is net.racing_connection
