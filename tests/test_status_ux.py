"""Connection status is useful to humans without intake, sends or extra authority."""
import json
import sqlite3

import pytest
from mcp.shared.memory import create_connected_server_and_client_session

from agents_over_telegram.core import Core, CoreError
from agents_over_telegram.mcp_server import create_server
from agents_over_telegram.onboarding import human_status
from agents_over_telegram.transport import FakeTransport


class Clock:
    def __init__(self):
        self.now = 1000.0

    def __call__(self):
        return self.now


@pytest.fixture
def anyio_backend():
    return 'asyncio'


@pytest.fixture
def environment(tmp_path):
    clock = Clock()
    transport = FakeTransport()
    core = Core(tmp_path / 'state', transport, clock=clock)
    core.register_account(transport.bot_id, transport.username)
    return core, transport, clock


def append(transport, text, chat=-100, topic=7, sender=101):
    number = max((update['update_id'] for update in transport.updates), default=0) + 1
    transport.updates.append({'update_id': number, 'message': {
        'message_id': number, 'chat': {'id': chat, 'type': 'supergroup', 'title': f'Group {chat}'},
        'message_thread_id': topic, 'from': {'id': sender, 'is_bot': False}, 'text': text}})


def bind(core, transport, label='Alpha', chat=-100, topic=7, sender=101):
    credentials = core.create_client(label)
    pairing = core.connection_begin(credentials['client_id'])
    append(transport, pairing['command'], chat, topic, sender)
    core.collect()
    core.confirm_pairing(pairing['pairing_id'])
    return credentials


def snapshot(core):
    with sqlite3.connect(core.database) as db:
        return '\n'.join(db.iterdump())


def never_called(*args, **kwargs):
    raise AssertionError('Status must not collect, send, lease, acknowledge or mutate work')


def test_unpaired_status_explains_shared_bot_and_on_demand_delivery(environment):
    core, transport, _ = environment
    credentials = core.create_client('Alpha')
    before = snapshot(core)
    status = core.connection_status(credentials['client_id'])
    assert status['label'] == 'Alpha'
    assert status['state'] == 'unpaired'
    assert status['next_action']['code'] == 'begin_pairing'
    assert 'existing shared bot' in status['next_action']['message']
    assert status['bot'] == {'id': transport.bot_id, 'username': transport.username}
    assert status['delivery'] == {'mode': 'on_demand', 'automatic': False,
                                  'scope': 'inbox_core', 'listener_status': 'not_checked'}
    assert status['history'] == {'window_hours': 24, 'kind': 'captured_only'}
    assert status['destination'] is None and status['connected'] is False
    assert status['allowed_senders'] == []
    assert status['intake'] == {'last_success_at': None, 'last_attempt_at': None,
                               'error': None, 'last_gap_at': None, 'possible_offline_gap': False}
    assert credentials['client_secret'] not in json.dumps(status)
    assert snapshot(core) == before
    assert transport.poll_offsets == []


def test_pairing_status_explains_send_then_private_confirmation_without_collecting(environment):
    core, transport, _ = environment
    client = core.create_client('Alpha')['client_id']
    pairing = core.connection_begin(client)
    append(transport, pairing['command'])
    before = snapshot(core)
    unobserved = core.connection_status(client)
    assert unobserved['state'] == 'pair_pending'
    assert unobserved['next_action']['code'] == 'send_pairing_command'
    assert unobserved['pairing']['sender_id'] is None
    assert transport.poll_offsets == []
    assert snapshot(core) == before
    core.collect()
    observed = core.connection_status(client)
    assert observed['state'] == 'pair_pending'
    assert observed['next_action']['code'] == 'confirm_pairing'
    assert observed['pairing']['sender_id'] == 101
    assert observed['destination'] is None
    assert 'locally' in observed['next_action']['message']


def test_expired_pairing_status_requires_new_pairing_without_consuming_record(environment):
    core, _, clock = environment
    client = core.create_client('Alpha')['client_id']
    core.connection_begin(client)
    clock.now += core.PAIR_TTL
    before = snapshot(core)
    status = core.connection_status(client)
    assert status['state'] == 'unpaired'
    assert status['pairing'] is None
    assert status['next_action']['code'] == 'begin_pairing'
    assert snapshot(core) == before


def test_connected_status_identifies_topic_and_numeric_allowlist(environment):
    core, transport, _ = environment
    client = bind(core, transport)['client_id']
    core.allow_sender(client, 55, is_bot=True)
    core.allow_sender(client, 77)
    status = core.connection_status(client)
    assert status['state'] == 'connected'
    assert status['destination'] == {'chat_id': -100, 'thread_id': 7, 'title': 'Group -100', 'owner_id': 101}
    assert status['allowed_senders'] == [
        {'sender_id': 55, 'is_bot': True}, {'sender_id': 77, 'is_bot': False}, {'sender_id': 101, 'is_bot': False}]
    assert status['intake']['last_success_at'] == 1000.0
    assert status['next_action']['code'] == 'check_messages'
    assert 'listener status is not checked here' in status['next_action']['message']


def test_connection_status_does_not_infer_or_start_a_listener(environment, monkeypatch):
    core, transport, _ = environment
    client = bind(core, transport)['client_id']
    before = snapshot(core)
    for method in ('collect', 'flush', 'poll', 'ack', 'send'):
        monkeypatch.setattr(core, method, never_called)
    status = core.connection_status(client)
    assert status['connected']
    assert status['delivery']['scope'] == 'inbox_core'
    assert status['delivery']['listener_status'] == 'not_checked'
    human = human_status(status)
    assert 'automatically through a running listener' in human
    assert 'Listener: not checked by connection status.' in human
    assert 'automatic delivery is off' not in human
    assert 'does not wake automatically' not in human
    assert snapshot(core) == before


def test_pause_resume_keeps_binding_and_guidance_does_not_request_reconnection(environment):
    core, transport, _ = environment
    client = bind(core, transport)['client_id']
    destination = core.connection_status(client)['destination']
    paused = core.pause(client)
    assert paused['state'] == 'paused'
    assert paused['next_action']['code'] == 'resume_connection'
    assert paused['connected'] is True and paused['paused'] is True
    assert 'pair' not in paused['next_action']['message']
    resumed = core.pause(client, False)
    assert resumed['state'] == 'connected'
    assert resumed['destination'] == destination
    assert resumed['pairing'] is None


def test_suspended_status_calls_for_owner_review_even_if_also_paused(environment):
    core, transport, _ = environment
    client = bind(core, transport)['client_id']
    next_id = max(update['update_id'] for update in transport.updates) + 1
    transport.updates.append({'update_id': next_id, 'my_chat_member': {
        'chat': {'id': -100}, 'new_chat_member': {'user': {'id': transport.bot_id}, 'status': 'left'}}})
    core.collect()
    status = core.pause(client)
    assert status['state'] == 'suspended'
    assert status['next_action']['code'] == 'review_connection'
    assert status['suspended'] == 'bot_removed_or_restricted'
    assert 'reconnect' not in status['next_action']['message']


def test_status_omits_other_clients_senders_destinations_and_messages(environment):
    core, transport, _ = environment
    a = bind(core, transport)
    b = bind(core, transport, label='Private Beta Label', chat=-200, topic=99, sender=202)
    core.allow_sender(b['client_id'], 987654)
    append(transport, 'private-beta-text', chat=-200, topic=99, sender=202)
    core.collect()
    status = core.connection_status(a['client_id'])
    encoded = json.dumps(status)
    for value in [b['client_id'], b['client_secret'], a['client_secret'], 'Private Beta Label',
                  'private-beta-text', '987654', 'Group -200']:
        assert value not in encoded
    assert status['allowed_senders'] == [{'sender_id': 101, 'is_bot': False}]
    assert status['pending_messages'] == 0
    assert status['permitted_peers'] == []


def test_status_keeps_leases_and_uncertain_sending_states_without_side_effects(environment, monkeypatch):
    core, transport, _ = environment
    client = bind(core, transport)['client_id']
    append(transport, 'Pending work')
    core.collect()
    leased = core.poll(client)
    transport.outcomes.append('unknown')
    unknown = core.send(client, 'Uncertain publication', 'unknown-status-test')
    queued = core.send(client, 'Interrupted publication', 'sending-status-test')
    with sqlite3.connect(core.database) as db:
        db.execute("UPDATE outbox SET status='sending' WHERE id=?", (queued['outbox_id'],))
    before = snapshot(core)
    for method in ('collect', 'flush', 'poll', 'ack', 'send'):
        monkeypatch.setattr(core, method, never_called)
    for method in ('get_updates', 'get_me', 'get_webhook_info', 'send_message'):
        monkeypatch.setattr(transport, method, never_called)
    status = core.connection_status(client)
    assert status['outbox'] == {'sending': 1, 'unknown': 1}
    assert status['pending_messages'] == 1
    assert status['next_action']['code'] == 'review_unknown_delivery'
    assert core.delivery_status(client, unknown['outbox_id'])['status'] == 'unknown'
    assert leased['lease_token'] in before
    assert snapshot(core) == before


def test_pending_work_has_a_useful_next_step(environment):
    core, transport, _ = environment
    client = bind(core, transport)['client_id']
    append(transport, 'Pending work')
    core.collect()
    status = core.connection_status(client)
    assert status['pending_messages'] == 1
    assert status['next_action']['code'] == 'poll_messages'
    assert status['state'] == 'connected'


def test_status_surfaces_failed_intake_without_leaking_webhook_value(environment):
    core, transport, _ = environment
    client = bind(core, transport)['client_id']
    secret = 'fake-webhook-secret-MUST-NOT-LEAK'
    transport.webhook = 'https://example.invalid/' + secret
    with pytest.raises(CoreError, match='webhook_conflict'):
        core.collect()
    before = snapshot(core)
    status = core.connection_status(client)
    assert status['intake']['error'] == 'webhook_conflict'
    assert status['intake']['last_success_at'] == 1000.0
    assert status['next_action']['code'] == 'review_intake'
    assert secret not in json.dumps(status)
    assert snapshot(core) == before


def test_status_exposes_possible_gap_without_claiming_complete_history(environment):
    core, transport, clock = environment
    client = bind(core, transport)['client_id']
    clock.now += 86400
    before = snapshot(core)
    status = core.connection_status(client)
    assert status['intake']['possible_offline_gap'] is True
    assert status['intake']['last_gap_at'] is None
    assert status['history']['kind'] == 'captured_only'
    assert status['next_action']['code'] == 'review_gap'
    assert snapshot(core) == before
    core.collect()
    status = core.connection_status(client)
    assert status['intake']['last_success_at'] == clock.now
    assert status['intake']['last_gap_at'] == clock.now
    assert status['intake']['possible_offline_gap'] is True


def test_disconnected_credentials_still_cannot_read_private_status(environment):
    core, transport, _ = environment
    credentials = bind(core, transport)
    client = credentials['client_id']
    disconnected = core.disconnect(client)
    assert disconnected == {'client_id': client, 'connected': False, 'revoked': True, 'queued_work_preserved': True}
    before = snapshot(core)
    with pytest.raises(CoreError, match='client_revoked'):
        core.connection_status(client)
    with pytest.raises(CoreError, match='authentication_failed'):
        core.authenticate(credentials['client_secret'])
    assert snapshot(core) == before


@pytest.mark.anyio
async def test_mcp_status_and_list_are_read_only_and_keep_fixed_client_scope(environment, monkeypatch):
    core, transport, _ = environment
    client = bind(core, transport)['client_id']
    before = snapshot(core)
    for method in ('collect', 'flush', 'poll', 'ack', 'send'):
        monkeypatch.setattr(core, method, never_called)
    async with create_connected_server_and_client_session(create_server(core, client)) as session:
        tools = {tool.name: tool for tool in (await session.list_tools()).tools}
        for name in ('connection_status', 'connections_list'):
            assert tools[name].annotations.readOnlyHint is True
            assert tools[name].annotations.openWorldHint is False
            result = await session.call_tool(name, {})
            assert result.isError is False
            assert result.structuredContent['result']['client_id'] == client
            assert result.structuredContent['result']['state'] == 'connected'
            assert result.structuredContent['result']['allowed_senders'] == [{'sender_id': 101, 'is_bot': False}]
        assert 'paused=false' in tools['connection_pause'].description
    assert snapshot(core) == before
