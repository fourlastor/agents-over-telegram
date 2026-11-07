"""Offline proof of owner_notify: private, labelled, forwarded, limited and idempotent."""
import sqlite3

import pytest

from agents_over_telegram.core import Core, CoreError
from agents_over_telegram.mcp_server import TOOLS, _error_body, call_tool
from agents_over_telegram.transport import FakeTransport, TransportError

OWNER = 101
GROUP = -100


class Clock:
    def __init__(self):
        self.now = 1000.0

    def __call__(self):
        return self.now

    def advance(self, seconds=2):
        self.now += seconds


def message(number, text, chat=GROUP, sender=OWNER):
    return {'update_id': number, 'message': {
        'message_id': number, 'chat': {'id': chat, 'type': 'supergroup', 'title': 'Test group'},
        'from': {'id': sender, 'is_bot': False, 'username': 'participant'}, 'text': text}}


def append(transport, text, **kwargs):
    number = max((u['update_id'] for u in transport.updates), default=0) + 1
    transport.updates.append(message(number, text, **kwargs))
    return number


@pytest.fixture
def env(tmp_path):
    clock = Clock()
    transport = FakeTransport()
    core = Core(tmp_path / 'state', transport, clock=clock)
    core.register_account(transport.bot_id, transport.username)
    return core, transport, clock


def bind(core, transport, label='Alpha', chat=GROUP, sender=OWNER):
    client_id = core.create_client(label)['client_id']
    pairing = core.connection_begin(client_id)
    append(transport, pairing['command'], chat=chat, sender=sender)
    core.collect()
    core.confirm_pairing(pairing['pairing_id'])
    return client_id


def owner_messages(transport):
    return [m for m in transport.sent if m['chat']['id'] == OWNER]


def test_sends_labelled_text_to_owner_private_chat(env):
    core, transport, _ = env
    client = bind(core, transport)
    result = core.owner_notify(client, 'Need your approval to merge.', 'n1')
    assert result['status'] == 'sent' and result['kind'] == 'owner_notify' and result['error'] is None
    [sent] = owner_messages(transport)
    # The private chat id is the owner's user id, recorded at pairing.
    assert sent['text'] == '[Agent: Alpha | Test group]\nNeed your approval to merge.'
    assert sent['message_thread_id'] is None and sent['reply_to_message_id'] is None
    assert core.delivery_status(client, result['notify_id']) == result


def test_forwards_a_captured_group_message_after_the_text(env):
    core, transport, _ = env
    client = bind(core, transport)
    number = append(transport, 'Seb, which option do you want?')
    core.collect()
    result = core.owner_notify(client, 'Unanswered question for you.', 'n1', forward_message_id=number)
    assert result['status'] == 'sent' and result['forwarded_message_id'] is not None
    assert transport.forwards == [{'chat_id': OWNER, 'from_chat_id': GROUP, 'message_id': number}]
    assert [m.get('text') for m in owner_messages(transport)][0].endswith('Unanswered question for you.')


def test_forwards_a_message_the_bot_published_in_the_destination(env):
    core, transport, _ = env
    client = bind(core, transport)
    published = core.send(client, 'Proposal: ship on Friday?', 'send-1')
    result = core.owner_notify(client, 'See my proposal.', 'n1', forward_message_id=published['message_id'])
    assert result['status'] == 'sent'
    assert transport.forwards[0]['message_id'] == published['message_id']


def test_refuses_forwarding_a_message_outside_the_destination(env):
    core, transport, _ = env
    client = bind(core, transport)
    other = bind(core, transport, 'Beta', chat=-200)
    foreign = append(transport, 'In another group', chat=-200)
    core.collect()
    for target in (foreign, 9999):
        with pytest.raises(CoreError) as error:
            core.owner_notify(client, 'Look', 'n-' + str(target), forward_message_id=target)
        assert error.value.code == 'unknown_forward_target'
    assert owner_messages(transport) == [] and other


def test_hourly_and_daily_limits_refuse_and_recover(env):
    core, transport, clock = env
    client = bind(core, transport)
    for index in range(3):
        assert core.owner_notify(client, f'decision {index}', f'h{index}')['status'] == 'sent'
        clock.advance(10)
    with pytest.raises(CoreError) as error:
        core.owner_notify(client, 'one more', 'h3')
    assert error.value.code == 'owner_notify_limit'
    assert error.value.details['limit'] == 3 and error.value.details['window_seconds'] == 3600
    assert error.value.details['retry_at'] == 1000.0 + 3600
    body = _error_body('owner_notify_limit', error.value.details)['error']
    assert body['code'] == 'owner_notify_limit' and 'Nothing was sent' in body['message']
    assert body['retry_at_utc'].endswith('Z')
    # After the hour, the first one leaves the hourly window.
    clock.now = 1000.0 + 3600 + 1
    assert core.owner_notify(client, 'one more', 'h3')['status'] == 'sent'
    # Fill the day: 10 per rolling day, at most 3 per rolling hour.
    sent = 4
    while sent < 10:
        clock.advance(3601)
        for _ in range(min(3, 10 - sent)):
            core.owner_notify(client, f'd{sent}', f'd{sent}')
            sent += 1
            clock.advance(1)
    clock.advance(3601)
    with pytest.raises(CoreError) as error:
        core.owner_notify(client, 'eleventh', 'd10')
    assert error.value.code == 'owner_notify_limit' and error.value.details['window_seconds'] == 86400
    assert error.value.details['sent_in_window'] == 10
    assert error.value.details['retry_at'] == 1000.0 + 86400
    clock.now = 1000.0 + 86400 + 1
    assert core.owner_notify(client, 'eleventh', 'd10')['status'] == 'sent'
    assert len(owner_messages(transport)) == 11


def test_idempotency_returns_same_outcome_and_refuses_changes(env):
    core, transport, _ = env
    client = bind(core, transport)
    first = core.owner_notify(client, 'Blocked on credentials.', 'same')
    assert core.owner_notify(client, 'Blocked on credentials.', 'same') == first
    assert len(owner_messages(transport)) == 1
    with pytest.raises(CoreError) as error:
        core.owner_notify(client, 'Different text.', 'same')
    assert error.value.code == 'idempotency_conflict'
    with pytest.raises(CoreError) as error:
        core.owner_notify(client, 'Blocked on credentials.', 'same', forward_message_id=1)
    assert error.value.code == 'idempotency_conflict'


def test_owner_chat_not_started_is_clear_and_not_counted(env):
    core, transport, clock = env
    client = bind(core, transport)
    transport.outcomes = ['reject'] * 5
    for index in range(5):
        clock.advance()
        with pytest.raises(CoreError) as error:
            core.owner_notify(client, 'Please decide.', f'k{index}')
        assert error.value.code == 'owner_chat_not_started'
    # The same key repeats the same refusal without a new attempt.
    with pytest.raises(CoreError):
        core.owner_notify(client, 'Please decide.', 'k0')
    body = _error_body('owner_chat_not_started', error.value.details)['error']
    assert '/start' in body['message'] and '@aot_test_bot' in body['message']
    # Five refusals do not use up the 3-per-hour limit.
    for index in range(3):
        clock.advance()
        assert core.owner_notify(client, 'Please decide.', f'ok{index}')['status'] == 'sent'


def test_forward_failure_after_the_text_keeps_the_text_sent(env):
    core, transport, _ = env
    client = bind(core, transport)
    number = append(transport, 'question')
    core.collect()
    transport.outcomes = ['sent', 'not_found']
    result = core.owner_notify(client, 'Look', 'n1', forward_message_id=number)
    assert result['status'] == 'sent' and result['error'] == 'forward_rejected'
    assert result['message_id'] is not None and result['forwarded_message_id'] is None


def test_unknown_forward_retry_repeats_only_the_forward(env):
    core, transport, clock = env
    client = bind(core, transport)
    number = append(transport, 'question')
    core.collect()
    transport.outcomes = ['sent', TransportError('http_failure')]
    result = core.owner_notify(client, 'Look', 'n1', forward_message_id=number)
    assert result['status'] == 'unknown' and result['message_id'] is not None
    texts = len([m for m in owner_messages(transport) if 'text' in m])
    # An unknown outcome still counts toward the limit.
    with sqlite3.connect(core.database) as db:
        assert db.execute("SELECT status FROM owner_outbox").fetchone()[0] == 'unknown'
    retried = core.resolve_unknown(result['notify_id'], 'retry')
    assert retried['status'] == 'queued'
    clock.advance()
    core.flush(client_id=client)
    final = core.delivery_status(client, result['notify_id'])
    assert final['status'] == 'sent' and final['forwarded_message_id'] is not None
    assert len([m for m in owner_messages(transport) if 'text' in m]) == texts
    assert len(transport.forwards) == 1


def test_paused_and_suspended_connections_refuse(env):
    core, transport, _ = env
    client = bind(core, transport)
    core.pause(client, True)
    with pytest.raises(CoreError) as error:
        core.owner_notify(client, 'x', 'p1')
    assert error.value.code == 'connection_paused'
    core.pause(client, False)
    with sqlite3.connect(core.database) as db:
        db.execute("UPDATE bindings SET suspended='bot_removed' WHERE client_id=?", (client,))
    with pytest.raises(CoreError) as error:
        core.owner_notify(client, 'x', 'p2')
    assert error.value.code == 'connection_suspended'
    assert owner_messages(transport) == []


def test_unpaired_agent_refuses(env):
    core, transport, _ = env
    client = core.create_client('Lonely')['client_id']
    with pytest.raises(CoreError) as error:
        core.owner_notify(client, 'x', 'u1')
    assert error.value.code == 'not_connected'


def test_tool_is_listed_with_schema_and_dispatches(env):
    core, transport, clock = env
    client = bind(core, transport)
    tool = {t.name: t for t in TOOLS}['owner_notify']
    schema = tool.inputSchema
    assert schema['required'] == ['text', 'idempotency_key']
    assert set(schema['properties']) == {'text', 'idempotency_key', 'forward_message_id'}
    assert 'decision' in tool.description and 'routine' in tool.description
    body = call_tool(core, client, 'owner_notify', {'text': 'Approve?', 'idempotency_key': 't1'})
    assert body['ok'] and body['result']['status'] == 'sent'
    assert call_tool(core, client, 'owner_notify', {'text': 'x'})['error']['code'] == 'invalid_arguments'
    assert call_tool(core, client, 'owner_notify', {'text': 'x', 'idempotency_key': 'k',
                                                    'forward_message_id': 0})['error']['code'] == 'invalid_arguments'
    clock.advance()
    transport.outcomes = ['reject']
    refused = call_tool(core, client, 'owner_notify', {'text': 'Approve?', 'idempotency_key': 't2'})
    assert refused['error']['code'] == 'owner_chat_not_started'
    assert 'send /start' in refused['error']['message']


def test_lazy_transport_passes_forward_message():
    from agents_over_telegram.cli import LazyTransport

    lazy = LazyTransport('unused', fake=True)
    lazy.transport = FakeTransport()
    result = lazy.forward_message(chat_id=OWNER, from_chat_id=GROUP, message_id=7)
    assert result['forwarded_from'] == {'chat_id': GROUP, 'message_id': 7}
