"""The relay API: tool calls answered by the always-on collector over the state directory's socket."""
import json
import socket

import pytest

from agents_over_telegram import cli, relay_api, serve
from agents_over_telegram.core import Core
from agents_over_telegram.transport import make_transport
from test_core import append, bind, env  # noqa: F401 (env is a fixture)
from test_setup import connect, prepare


@pytest.fixture
def relay(env):
    core, transport, clock = env
    running = relay_api.Relay(core, core.state_dir)
    running.start()
    yield running
    running.stop()


def test_a_call_runs_on_the_relay(env, relay):
    core, transport, clock = env
    client, credentials = bind(core, transport)
    append(transport, 'hello')
    core.collect()
    body = relay_api.call(core.state_dir, credentials['client_secret'], 'messages_poll', {})
    assert body['ok'] is True
    assert [d['text'] for d in body['result']['deliveries']] == ['hello']
    assert body['result']['message_content_trust'] == 'untrusted'


def test_the_socket_is_private(env, relay):
    core, transport, clock = env
    path = relay_api.socket_path(core.state_dir)
    assert oct(path.stat().st_mode & 0o777) == '0o600'
    assert oct(path.parent.stat().st_mode & 0o777) == '0o700'


def test_a_wrong_secret_reaches_no_inbox(env, relay):
    core, transport, clock = env
    bind(core, transport)
    body = relay_api.call(core.state_dir, 'not-a-secret', 'messages_poll', {})
    assert body['ok'] is False and body['error']['code'] == 'authentication_failed'


def test_invalid_arguments_are_refused_by_the_relay_too(env, relay):
    core, transport, clock = env
    client, credentials = bind(core, transport)
    body = relay_api.call(core.state_dir, credentials['client_secret'], 'message_send', {'text': 'x'})
    assert body['error']['code'] == 'invalid_arguments'


def test_no_relay_means_the_node_runs_the_call(env):
    core, transport, clock = env
    with pytest.raises(relay_api.Unavailable):
        relay_api.call(core.state_dir, 'secret', 'messages_poll', {})


def raw(state, request):
    connection = socket.socket(socket.AF_UNIX)
    connection.connect(str(relay_api.socket_path(state)))
    connection.sendall(json.dumps(request).encode() + b'\n')
    answer = json.loads(relay_api._read_line(connection, 1 << 20))
    connection.close()
    return answer


def test_protocol_versions_are_checked(env, relay):
    core, transport, clock = env
    assert raw(core.state_dir, {'protocol': 0})['error'] == 'node_too_old'
    assert raw(core.state_dir, {'protocol': relay_api.PROTOCOL + 1})['error'] == 'relay_too_old'
    assert raw(core.state_dir, {'protocol': 'one'})['error'] == 'bad_request'


def test_an_older_node_is_told_to_update(env, relay, monkeypatch):
    core, transport, clock = env
    # A node that speaks protocol 0, against a relay that needs at least 1.
    monkeypatch.setattr(relay_api, 'PROTOCOL', 0)
    body = relay_api.call(core.state_dir, 'secret', 'messages_poll', {})
    assert body['error']['code'] == 'node_too_old'


def test_a_newer_node_runs_the_call_itself_on_an_older_relay(env, relay, monkeypatch):
    core, transport, clock = env
    monkeypatch.setattr(relay, 'answer', lambda request: {'protocol': 1, 'ok': False, 'error': 'relay_too_old'})
    with pytest.raises(relay_api.Unavailable):
        relay_api.call(core.state_dir, 'secret', 'messages_poll', {})


def test_a_lost_answer_is_reported_not_retried(env, relay, monkeypatch):
    core, transport, clock = env
    calls = []

    def hang_up(connection):
        calls.append(1)
        connection.close()
    monkeypatch.setattr(relay, 'handle', hang_up)
    body = relay_api.call(core.state_dir, 'secret', 'message_send', {'text': 'x', 'idempotency_key': 'k'})
    assert body['error']['code'] == 'relay_no_answer' and calls == [1]


def test_a_stale_socket_is_replaced_but_a_live_relay_is_kept(env, relay):
    core, transport, clock = env
    with pytest.raises(OSError):
        relay_api.Relay(core, core.state_dir).start()
    relay.server.shutdown()
    relay.server.server_close()
    relay.server = None
    # The socket file is still there, with nobody listening.
    assert relay_api.socket_path(core.state_dir).exists()
    again = relay_api.Relay(core, core.state_dir)
    again.start()
    again.stop()
    assert not relay_api.socket_path(core.state_dir).exists()


def test_another_users_socket_path_is_never_replaced(env):
    core, transport, clock = env
    path = relay_api.socket_path(core.state_dir)
    path.parent.mkdir(mode=0o700)
    path.write_text('not a socket')
    with pytest.raises(OSError):
        relay_api.Relay(core, core.state_dir).start()
    assert path.read_text() == 'not a socket'


def test_serve_answers_agents_while_it_collects(env, monkeypatch):
    core, transport, clock = env
    client, credentials = bind(core, transport)
    seen = []

    def keep_running():
        if not seen:
            seen.append(relay_api.call(core.state_dir, credentials['client_secret'], 'connection_status', {}))
            return True
        return False
    monkeypatch.setattr(core, 'collect', lambda **kw: {'status': 'ok'})
    monkeypatch.setattr(core, 'flush', lambda **kw: None)
    serve.run(core, core.state_dir, poll_timeout=0, sleep=lambda s: None, keep_running=keep_running,
              log=lambda t: None, relay=relay_api.Relay(core, core.state_dir))
    assert seen[0]['ok'] is True and seen[0]['result']['label'] == 'Alpha'
    assert not relay_api.socket_path(core.state_dir).exists()


def test_serve_keeps_collecting_when_it_cannot_listen(env, monkeypatch):
    core, transport, clock = env
    logs = []

    class Broken:
        log = None

        def start(self):
            raise OSError('no')

        def stop(self):
            raise AssertionError('never started')
    monkeypatch.setattr(core, 'collect', lambda **kw: {'status': 'ok'})
    monkeypatch.setattr(core, 'flush', lambda **kw: None)
    left = [1]

    def keep_running():
        left[0] -= 1
        return left[0] >= 0
    assert serve.run(core, core.state_dir, poll_timeout=0, sleep=lambda s: None, keep_running=keep_running,
                     log=logs.append, relay=Broken()) == 0
    assert any('not answering agents' in line for line in logs)


def test_aot_tool_goes_through_the_relay(tmp_path, monkeypatch):
    result = prepare(tmp_path)
    connect(result)
    state = result['state_dir']
    core = Core(state, make_transport(state, fake=True), recover_sends=False)
    running = relay_api.Relay(core, state)
    answered = []
    original = running.answer
    monkeypatch.setattr(running, 'answer', lambda request: answered.append(request['tool']) or original(request))
    running.start()
    try:
        monkeypatch.chdir(result['project_dir'])
        monkeypatch.setattr(cli, '_tool_arguments', lambda: {})
        body = cli.run(cli.parser().parse_args(['--state-dir', str(state), '--fake', 'tool', 'connection_status',
                                                '--client-file', result['client_file']]))
    finally:
        running.stop()
    assert answered == ['connection_status'] and body['ok'] is True
    assert body['result']['label'] == 'Alpha'


def test_the_mcp_server_falls_back_only_when_no_relay_took_the_call():
    from agents_over_telegram.mcp_server import relayed

    def unavailable(name, arguments):
        raise relay_api.Unavailable('no_relay')
    assert relayed(unavailable, 'messages_poll', {}) is None
    assert relayed(None, 'messages_poll', {}) is None
    body = {'ok': False, 'error': {'code': 'relay_no_answer'}}
    assert relayed(lambda name, arguments: body, 'message_send', {}) is body


def test_a_waiter_without_a_relay_falls_back(env):
    core, transport, clock = env
    assert relay_api.Waiter(core.state_dir, 'secret')(1) is False


def test_a_waiter_blocks_until_the_relay_has_news(env, relay):
    import threading
    import time
    core, transport, clock = env
    client, credentials = bind(core, transport)
    waiter = relay_api.Waiter(core.state_dir, credentials['client_secret'])
    # The first wait only learns the relay's position.
    assert waiter(5) is True and waiter.instance == relay.instance
    threading.Timer(0.2, relay.notify).start()
    started = time.monotonic()
    assert waiter(10) is True
    assert time.monotonic() - started < 5 and waiter.generation == relay.generation
    started = time.monotonic()
    assert waiter(0.3) is True and time.monotonic() - started >= 0.25


def test_a_wait_needs_the_client_secret(env, relay):
    core, transport, clock = env
    bind(core, transport)
    assert relay_api.Waiter(core.state_dir, 'wrong')(1) is False


def test_stopping_the_relay_releases_waiters(env):
    import threading
    import time
    core, transport, clock = env
    client, credentials = bind(core, transport)
    running = relay_api.Relay(core, core.state_dir)
    running.start()
    waiter = relay_api.Waiter(core.state_dir, credentials['client_secret'])
    waiter(1)
    threading.Timer(0.2, running.stop).start()
    started = time.monotonic()
    waiter(30)
    assert time.monotonic() - started < 10


def test_a_send_through_the_relay_wakes_waiters(env, relay):
    core, transport, clock = env
    client, credentials = bind(core, transport)
    before = relay.generation
    body = relay_api.call(core.state_dir, credentials['client_secret'], 'message_send',
                          {'text': 'hi', 'idempotency_key': 'k1'})
    assert body['ok'] is True and relay.generation == before + 1


def test_serve_wakes_waiters_after_routing_updates(env, monkeypatch):
    core, transport, clock = env
    outcomes = [{'status': 'ok', 'updates': 0}, {'status': 'ok', 'updates': 2}]
    monkeypatch.setattr(core, 'collect', lambda **kw: outcomes.pop(0))
    monkeypatch.setattr(core, 'flush', lambda **kw: None)
    notified = []

    class Recorder:
        log = None

        def start(self):
            pass

        def stop(self):
            pass

        def notify(self):
            notified.append(1)
    left = [2]

    def keep_running():
        left[0] -= 1
        return left[0] >= 0
    serve.run(core, core.state_dir, poll_timeout=0, sleep=lambda s: None, keep_running=keep_running,
              log=lambda t: None, relay=Recorder())
    assert notified == [1]


def test_a_relayed_watcher_neither_collects_nor_sleeps(env, monkeypatch):
    from agents_over_telegram.push import watch
    core, transport, clock = env
    client, _ = bind(core, transport)
    append(transport, 'hello')
    core.collect()
    monkeypatch.setattr(core, 'collect', lambda **kw: (_ for _ in ()).throw(AssertionError('collected')))
    events, waits = [], []
    result = watch(core, client, events.append, collect=True, duration=0, sleep=lambda s: (_ for _ in ()).throw(
        AssertionError('slept')), keep_running=lambda: len(waits) < 2, wait_for_change=lambda t: waits.append(t) or True)
    assert result['reason'] == 'parent_exited' and len(events) == 1 and 'telegram_messages' == events[0]['type']


def test_a_watcher_collects_again_when_the_relay_is_gone(env, monkeypatch):
    from agents_over_telegram.push import watch
    core, transport, clock = env
    client, _ = bind(core, transport)
    collected = []
    monkeypatch.setattr(core, 'collect', lambda **kw: collected.append(kw) or {'status': 'ok'})
    runs = [0]

    def keep_running():
        runs[0] += 1
        return runs[0] <= 2
    watch(core, client, lambda e: None, collect=True, duration=0, sleep=lambda s: None,
          keep_running=keep_running, wait_for_change=lambda t: False)
    assert len(collected) == 2


@pytest.fixture
def short_tmp(monkeypatch):
    import shutil
    import tempfile
    from pathlib import Path
    base = Path(tempfile.mkdtemp(prefix='aot', dir='/tmp'))
    monkeypatch.setattr(relay_api, '_short_base', lambda: base / 'u')
    yield base
    shutil.rmtree(base, ignore_errors=True)


def test_a_long_state_path_gets_a_short_private_socket(tmp_path, monkeypatch, short_tmp):
    monkeypatch.delenv('XDG_RUNTIME_DIR', raising=False)
    monkeypatch.delenv('TMPDIR', raising=False)
    deep = tmp_path / ('d' * 60) / ('e' * 60) / 'state'
    path = relay_api.socket_path(deep)
    assert path.parent.parent == short_tmp / 'u' and len(str(path)) <= relay_api.MAX_SOCKET_PATH
    assert relay_api.socket_path(deep) == path
    # A private runtime directory is preferred when it is short enough.
    run = short_tmp / 'run'
    run.mkdir(mode=0o700)
    for name in ('XDG_RUNTIME_DIR', 'TMPDIR'):
        monkeypatch.setenv(name, str(run))
        assert relay_api.socket_path(deep).parent.parent == run
        monkeypatch.delenv(name)
    # Not when it is shared, missing or too long.
    shared = short_tmp / 'shared'
    shared.mkdir()
    shared.chmod(0o777)
    for value in (shared, short_tmp / 'missing', tmp_path / ('r' * 80)):
        monkeypatch.setenv('TMPDIR', str(value))
        assert relay_api.socket_path(deep).parent.parent == short_tmp / 'u'
    short_state = short_tmp / 's'
    assert relay_api.socket_path(short_state) == short_state / 'relay' / 'relay.sock'


def test_a_relay_works_for_a_long_state_path(tmp_path, monkeypatch, short_tmp):
    monkeypatch.delenv('XDG_RUNTIME_DIR', raising=False)
    monkeypatch.delenv('TMPDIR', raising=False)
    from test_core import Clock
    from agents_over_telegram.transport import FakeTransport
    transport = FakeTransport()
    core = Core(tmp_path / ('d' * 60) / ('e' * 60) / 'state', transport, clock=Clock())
    core.register_account(transport.bot_id, transport.username)
    client, credentials = bind(core, transport)
    running = relay_api.Relay(core, core.state_dir)
    running.start()
    try:
        body = relay_api.call(core.state_dir, credentials['client_secret'], 'connection_status', {})
    finally:
        running.stop()
    assert body['ok'] is True
