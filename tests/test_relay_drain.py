"""The relay finishes requests in flight before it stops; owner-action answers retry once and log their errors."""
import socket
import threading
import time
import urllib.error

import pytest

from agents_over_telegram import relay_api
from agents_over_telegram.core import _owner_call
from agents_over_telegram.transport import TelegramTransport, TransportError
from test_core import bind, env  # noqa: F401 (env is a fixture)


@pytest.mark.skipif(not hasattr(socket, 'AF_UNIX'), reason='needs Unix sockets')
def test_a_request_in_flight_finishes_before_the_relay_stops(env, monkeypatch):
    core, transport, clock = env
    client, credentials = bind(core, transport)
    relay = relay_api.Relay(core, core.state_dir)
    original = relay.answer
    started = threading.Event()

    def slow(request):
        started.set()
        time.sleep(0.5)
        return original(request)
    monkeypatch.setattr(relay, 'answer', slow)
    relay.start()
    results = []
    caller = threading.Thread(target=lambda: results.append(
        relay_api.call(core.state_dir, credentials['client_secret'], 'connection_status', {})))
    caller.start()
    assert started.wait(5)
    relay.stop()
    caller.join(10)
    assert results and results[0]['ok'] is True


def test_the_drain_gives_up_after_its_limit(env, monkeypatch):
    core, transport, clock = env
    relay = relay_api.Relay(core, core.state_dir)
    monkeypatch.setattr(relay_api, 'DRAIN_SECONDS', 0.2)
    relay.connections.acquire()  # a request that never finishes
    started = time.monotonic()
    relay._drain()
    assert time.monotonic() - started < 2


def test_a_timed_out_answer_is_tried_once_more():
    outcomes = [TransportError('timeout'), None]

    def call():
        outcome = outcomes.pop(0)
        if outcome:
            raise outcome
    calls = {}
    _owner_call(calls, 'answer', call)
    assert calls['answer_attempts'] == 2 and 'answer_error' not in calls and 'answer_ms' in calls


def test_a_rejected_answer_is_not_retried_and_its_error_is_kept():
    attempts = []

    def call():
        attempts.append(1)
        raise TransportError(400)
    calls = {}
    _owner_call(calls, 'answer', call)
    assert attempts == [1] and calls['answer_error'] == '400'


def test_a_timeout_is_named_apart_from_other_failures():
    class Opener:
        def __init__(self, error):
            self.error = error

        def open(self, request, timeout=None):
            raise self.error
    timed_out = TelegramTransport('123456:TEST_TOKEN_abcdefghijklmnop', opener=Opener(urllib.error.URLError(TimeoutError())))
    with pytest.raises(TransportError) as error:
        timed_out.answer_widget_query('q', '{}')
    assert error.value.code == 'timeout'
    refused = TelegramTransport('123456:TEST_TOKEN_abcdefghijklmnop', opener=Opener(ConnectionRefusedError()))
    with pytest.raises(TransportError) as error:
        refused.answer_widget_query('q', '{}')
    assert error.value.code == 'transport_failure'
