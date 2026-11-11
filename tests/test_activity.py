"""The bounded activity log: collector cycles, owner actions, relay calls and collector events, with timings."""
import json
import socket

import pytest

from agents_over_telegram import relay_api, serve
from test_core import append, bind, env  # noqa: F401 (env is a fixture)
from test_owner_approval import ask, press
from test_settings_panel import k, private_message, query


def kinds(core, kind):
    return [entry for entry in core.activity() if entry['kind'] == kind]


def test_a_cycle_that_routes_updates_is_logged_with_its_steps(env):
    core, transport, clock = env
    client, _ = bind(core, transport)
    append(transport, 'hello')
    core.collect(timeout=1)
    [cycle] = kinds(core, 'cycle')[-1:]
    assert cycle['updates'] == 1 and cycle['kinds'] == {'message': 1} and cycle['long_poll'] is True
    assert 'work_ms' in cycle and isinstance(cycle['steps_ms'], dict)
    # The time since the previous fetch returned shows how long new updates had to wait.
    assert cycle['gap_ms'] is not None


def test_a_quiet_fast_cycle_is_not_logged(env):
    core, transport, clock = env
    bind(core, transport)
    before = len(kinds(core, 'cycle'))
    core.collect()
    assert len(kinds(core, 'cycle')) == before


def test_a_slow_quiet_cycle_is_logged(env, monkeypatch):
    core, transport, clock = env
    bind(core, transport)
    monkeypatch.setattr(core, 'SLOW_CYCLE_MS', 0)
    before = len(kinds(core, 'cycle'))
    core.collect()
    assert len(kinds(core, 'cycle')) == before + 1 and kinds(core, 'cycle')[-1]['updates'] == 0


def test_a_failed_cycle_is_logged(env, monkeypatch):
    core, transport, clock = env
    from agents_over_telegram.transport import TransportError

    def fail(**kw):
        raise TransportError('down')
    monkeypatch.setattr(transport, 'get_updates', fail)
    with pytest.raises(Exception):
        core.collect()
    assert kinds(core, 'cycle')[-1]['error'] == 'telegram_intake_failed'


def test_a_panel_tap_is_logged_with_its_latency(env):
    core, transport, clock = env
    client, _ = bind(core, transport)
    message_id = core.send_settings_panel(client)['message_id']
    query(transport, message_id, f'v:{k(client)}')
    core.collect()
    [tap] = [e for e in kinds(core, 'owner') if e['action'] == 'panel_tap']
    assert tap['answer'] == 'view' and tap['routed_to_answered_ms'] >= 0 and 'answer_ms' in tap


def test_a_settings_command_is_logged(env):
    core, transport, clock = env
    bind(core, transport)
    private_message(transport, '/settings')
    core.collect()
    [command] = [e for e in kinds(core, 'owner') if e['action'] == 'settings_command']
    assert command['outcome'] == 'sent' and 'send_ms' in command


def test_a_button_press_is_logged(env):
    core, transport, clock = env
    client, _ = bind(core, transport)
    approval = ask(core, client, delivery_filter='addressed')
    press(transport, approval['approval_id'])
    core.collect()
    assert [e['action'] for e in kinds(core, 'owner')][-1] == 'button_press'


def test_each_kind_keeps_its_last_entries(env, monkeypatch):
    core, transport, clock = env
    monkeypatch.setattr(core, 'ACTIVITY_KEEP', 3)
    for number in range(5):
        core.record_activity('relay', {'n': number})
    core.record_activity('serve', {'event': 'started'})
    assert [e['n'] for e in kinds(core, 'relay')] == [2, 3, 4]
    assert len(kinds(core, 'serve')) == 1


def test_no_message_text_reaches_the_log(env):
    core, transport, clock = env
    bind(core, transport)
    append(transport, 'a very private message body')
    core.collect(timeout=1)
    assert 'very private' not in json.dumps(core.activity())


@pytest.mark.skipif(not hasattr(socket, 'AF_UNIX'), reason='needs Unix sockets')
def test_relay_calls_are_logged_but_waits_are_not(env):
    core, transport, clock = env
    client, credentials = bind(core, transport)
    relay = relay_api.Relay(core, core.state_dir)
    relay.start()
    try:
        relay_api.call(core.state_dir, credentials['client_secret'], 'connection_status', {})
        relay_api.Waiter(core.state_dir, credentials['client_secret'])(0.1)
    finally:
        relay.stop()
    [call] = kinds(core, 'relay')
    assert call['call'] == 'connection_status' and call['outcome'] == 'ok' and call['ms'] >= 0


def test_collector_events_are_logged(env, monkeypatch):
    core, transport, clock = env
    monkeypatch.setattr(core, 'collect', lambda **kw: {'status': 'ok'})
    monkeypatch.setattr(core, 'flush', lambda **kw: None)
    monkeypatch.setenv('AOT_SERVE_SUPERVISED', '1')
    serve.run(core, core.state_dir, poll_timeout=0, sleep=lambda s: None, log=lambda t: None)
    events = [e['event'] for e in kinds(core, 'serve')]
    assert events[0] == 'started' and 'service was removed' in events[-1]


def test_activity_lines_are_readable(env):
    core, transport, clock = env
    core.record_activity('owner', {'action': 'panel_tap', 'routed_to_answered_ms': 42})
    [line] = serve.activity_lines(core.activity())
    assert 'owner' in line and 'action=panel_tap' in line and 'routed_to_answered_ms=42' in line


def test_the_cli_prints_the_activity(env, capsys):
    from agents_over_telegram.cli import parser, run
    core, transport, clock = env
    core.record_activity('serve', {'event': 'started'})
    run(parser().parse_args(['--state-dir', str(core.state_dir), '--fake', 'serve', '--activity']))
    assert 'event=started' in capsys.readouterr().out
