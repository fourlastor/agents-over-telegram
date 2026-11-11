"""Agents use the relay for hooks, inbox claims, tool calls and startup, and open the database only without one."""
import argparse
import json
import socket
from pathlib import Path

import pytest

from agents_over_telegram import cli, hosts, mcp_server, push, relay_api
from agents_over_telegram.core import Core
from agents_over_telegram.transport import make_transport
from test_hosts import connected, receive

pytestmark = pytest.mark.skipif(not hasattr(socket, 'AF_UNIX'), reason='needs Unix sockets')


@pytest.fixture
def project(tmp_path, monkeypatch):
    result = connected(tmp_path)
    state = Path(result['state_dir'])
    core = Core(state, make_transport(state, fake=True), recover_sends=False)
    relay = relay_api.Relay(core, state)
    relay.start()
    yield result, relay
    relay.stop()


def no_local_database(monkeypatch):
    def refuse(*args, **kwargs):
        raise AssertionError('an agent opened the database although a relay answers')
    monkeypatch.setattr(hosts, 'Core', refuse, raising=False)
    monkeypatch.setattr('agents_over_telegram.core.Core.__init__', refuse)


def secret(result):
    return json.loads(Path(result['client_file']).read_text(encoding='utf-8'))['client_secret']


def test_a_hook_is_answered_by_the_relay(project, monkeypatch):
    result, relay = project
    receive(result, 'hello from the group')
    no_local_database(monkeypatch)
    args = argparse.Namespace(host='claude', project_dir=result['project_dir'], collect_interval=0)
    answer = hosts.hook_answer(args, json.dumps({'hook_event_name': 'UserPromptSubmit', 'prompt': 'private'}))
    assert '1 new Telegram message' in answer['hookSpecificOutput']['additionalContext']


def test_only_the_fields_the_hook_needs_reach_the_relay(project, monkeypatch):
    result, relay = project
    seen = []
    original = relay._node_op
    monkeypatch.setattr(relay, '_node_op', lambda op, client_id, request: seen.append(request) or
                        original(op, client_id, request))
    hosts._relayed_hook(Path(result['state_dir']), Path(result['client_file']), 'PostToolUse',
                        {'tool_name': 'Bash', 'tool_input': {'command': 'cat secrets'}, 'prompt': 'private'})
    assert seen[0]['payload'] == {'tool_name': 'Bash'}


def test_a_waiter_claims_through_the_relay(project, monkeypatch):
    result, relay = project
    receive(result, 'hello')
    node = relay_api.Node(result['state_dir'], secret(result))
    opened = []
    core = relay_api.LazyCore(lambda: opened.append(1))
    events = []
    rounds = [0]

    def keep_running():
        rounds[0] += 1
        return rounds[0] <= 2
    push.watch(core, json.loads(Path(result['client_file']).read_text(encoding='utf-8'))['client_id'],
               events.append, collect=True, duration=0, keep_running=keep_running, remote=node)
    assert [e['type'] for e in events] == ['telegram_messages'] and opened == []


def test_a_relay_that_predates_an_operation_counts_as_unavailable(project, monkeypatch):
    result, relay = project
    monkeypatch.setattr(relay, '_node_op', lambda op, client_id, request: {
        'protocol': relay_api.PROTOCOL, 'ok': False, 'error': 'bad_request'})
    with pytest.raises(relay_api.Unavailable):
        relay_api.Node(result['state_dir'], secret(result)).claim('watch')


def test_a_revoked_client_hears_it_from_the_relay(project):
    result, relay = project
    from agents_over_telegram.core import CoreError
    with pytest.raises(CoreError) as error:
        relay_api.Node(result['state_dir'], 'not-the-secret').claim('watch')
    assert error.value.code == 'authentication_failed'


def test_aot_tool_never_opens_the_database_with_a_relay(project, monkeypatch):
    result, relay = project
    no_local_database(monkeypatch)
    monkeypatch.chdir(result['project_dir'])
    monkeypatch.setattr(cli, '_tool_arguments', lambda: {})
    body = cli.run(cli.parser().parse_args(['--state-dir', result['state_dir'], '--fake', 'tool', 'connection_status',
                                            '--client-file', result['client_file']]))
    assert body['ok'] is True


def test_the_mcp_server_starts_without_opening_the_database(project, monkeypatch):
    result, relay = project
    no_local_database(monkeypatch)
    created = []
    monkeypatch.setattr(mcp_server, 'create_server', lambda core, client_id, client_file, forward=None:
                        created.append((core, client_id)) or object())

    async def capture(server):
        pass
    monkeypatch.setattr(mcp_server, '_run_stdio', capture)
    mcp_server.serve(result['state_dir'], result['client_file'], transport=object())
    core, client_id = created[0]
    assert core.opened is False and client_id == result['client_id']


def test_without_a_relay_agents_still_work_locally(tmp_path):
    result = connected(tmp_path)
    receive(result, 'hello')
    args = argparse.Namespace(host='claude', project_dir=result['project_dir'], collect_interval=0)
    answer = hosts.hook_answer(args, json.dumps({'hook_event_name': 'UserPromptSubmit'}))
    assert '1 new Telegram message' in answer['hookSpecificOutput']['additionalContext']
