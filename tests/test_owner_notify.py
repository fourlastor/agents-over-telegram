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


def bind(core, transport, label='Alpha', chat=GROUP, sender=OWNER, enable=True):
    client_id = core.create_client(label)['client_id']
    pairing = core.connection_begin(client_id)
    append(transport, pairing['command'], chat=chat, sender=sender)
    core.collect()
    core.confirm_pairing(pairing['pairing_id'])
    if enable:
        core.delivery_settings(client_id, owner_notify=True)
    return client_id


def private(transport, text, sender=OWNER):
    number = max((u['update_id'] for u in transport.updates), default=0) + 1
    transport.updates.append({'update_id': number, 'message': {
        'message_id': number, 'chat': {'id': sender, 'type': 'private', 'first_name': 'Owner'},
        'from': {'id': sender, 'is_bot': False, 'username': 'owner'}, 'text': text}})


def owner_messages(transport):
    return [m for m in transport.sent if m['chat']['id'] == OWNER]


def test_sends_labelled_text_to_owner_private_chat(env):
    core, transport, _ = env
    client = bind(core, transport)
    result = core.owner_notify(client, 'Need your approval to merge.', 'n1')
    assert result['status'] == 'sent' and result['kind'] == 'owner_notify' and result['error'] is None
    [sent] = owner_messages(transport)
    # The private chat id is the owner's user id, recorded at pairing.
    assert sent['text'] == ('[Agent: Alpha | Test group]\nNeed your approval to merge.\n'
                            'Reply to this message to answer the agent.')
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
    assert 'Unanswered question for you.' in [m.get('text') for m in owner_messages(transport)][0]


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
    assert set(schema['properties']) == {'text', 'idempotency_key', 'forward_message_id', 'reply_to'}
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


def test_off_by_default_and_refusal_is_clear_and_not_counted(env):
    core, transport, clock = env
    client = bind(core, transport, enable=False)
    assert core.connection_status(client)['routing']['owner_notify'] is False
    for index in range(5):
        with pytest.raises(CoreError) as error:
            core.owner_notify(client, 'Approve?', f'off{index}')
        assert error.value.code == 'owner_notify_disabled'
    body = _error_body('owner_notify_disabled')['error']
    assert 'aot delivery-settings --owner-notify on' in body['message'] and 'owner decides' in body['message']
    assert owner_messages(transport) == []
    with sqlite3.connect(core.database) as db:
        assert db.execute('SELECT count(*) FROM owner_outbox').fetchone()[0] == 0
    settings = core.delivery_settings(client, owner_notify=True)
    assert settings['owner_notify'] is True
    assert core.connection_status(client)['routing']['owner_notify'] is True
    # Earlier refusals used no part of the limit.
    for index in range(3):
        clock.advance()
        assert core.owner_notify(client, 'Approve?', f'on{index}')['status'] == 'sent'
    core.delivery_settings(client, owner_notify=False)
    with pytest.raises(CoreError, match='owner_notify_disabled'):
        core.owner_notify(client, 'Approve?', 'off-again')
    with pytest.raises(CoreError, match='invalid_delivery_settings'):
        core.delivery_settings(client, owner_notify='on')


def test_existing_bindings_migrate_with_owner_notify_off(env, tmp_path):
    core, transport, clock = env
    client = bind(core, transport, enable=False)
    with sqlite3.connect(core.database) as db:
        db.execute('ALTER TABLE bindings DROP COLUMN owner_notify')
    core = Core(core.state_dir, transport, clock=clock)
    assert core.connection_status(client)['routing']['owner_notify'] is False


def test_delivery_settings_cli_flag():
    from agents_over_telegram.cli import parser

    args = parser().parse_args(['delivery-settings', '--client-file', 'c.json', '--owner-notify', 'on'])
    assert args.owner_notify == 'on'
    both = parser().parse_args(['delivery-settings', '--client-file', 'c.json', '--owner-notify', 'off',
                                '--wake-interval', '600', '--wake-exempt', 'owner'])
    assert (both.owner_notify, both.wake_interval, both.wake_exempt) == ('off', 600, 'owner')
    assert parser().parse_args(['delivery-settings', '--client-file', 'c.json']).owner_notify is None
    with pytest.raises(SystemExit):
        parser().parse_args(['delivery-settings', '--client-file', 'c.json', '--owner-notify', 'yes'])


def test_private_chat_messages_are_unassigned_with_their_own_reason(env):
    core, transport, _ = env
    client = bind(core, transport)
    private(transport, '/start')
    private(transport, 'Yes, go ahead.')
    assert core.collect()['status'] == 'ok'
    reasons = [row['reason'] for row in core.unassigned()['updates']]
    assert reasons.count('private_chat') == 2
    assert 'unsupported_destination' not in reasons
    # Nothing reaches any agent, and no text is kept.
    assert core.poll(client)['deliveries'] == []
    with sqlite3.connect(core.database) as db:
        assert db.execute("SELECT chat_id FROM unmatched WHERE reason='private_chat'").fetchall() == [(None,), (None,)]


def private_reaction(transport, message_id, sender=OWNER, chat=OWNER, emoji='👍'):
    number = max((u['update_id'] for u in transport.updates), default=0) + 1
    transport.updates.append({'update_id': number, 'message_reaction': {
        'chat': {'id': chat, 'type': 'private', 'first_name': 'Owner'}, 'message_id': message_id, 'date': 1000,
        'user': {'id': sender, 'is_bot': False, 'username': 'owner'},
        'old_reaction': [], 'new_reaction': [{'type': 'emoji', 'emoji': emoji}]}})
    return number


def private_reasons(core):
    return [row['reason'] for row in core.unassigned()['updates']]


def test_owner_reaction_on_a_notice_reaches_the_sending_agent(env):
    core, transport, clock = env
    sender = bind(core, transport)
    other = bind(core, transport, 'Beta', chat=-200)
    notice = core.owner_notify(sender, 'Approve the release?', 'n1')
    clock.advance()
    core.owner_notify(other, 'Unrelated question', 'n2')
    private_reaction(transport, notice['message_id'])
    assert core.collect()['status'] == 'ok'
    [delivery] = core.poll(sender)['deliveries']
    assert delivery['kind'] == 'reaction' and delivery['own_message'] is True
    assert delivery['chat_id'] == OWNER and delivery['thread_id'] is None
    assert delivery['message_id'] == notice['message_id']
    assert delivery['new_reaction'] == ['👍']
    assert core.poll(other)['deliveries'] == []
    assert 'private_chat' not in private_reasons(core)


def test_owner_reaction_on_a_forward_reaches_the_sending_agent(env):
    core, transport, _ = env
    sender = bind(core, transport)
    number = append(transport, 'Seb, which option?')
    core.collect()
    core.poll(sender)  # lease the group message, so only the reaction is new below
    notice = core.owner_notify(sender, 'Look at this.', 'n1', forward_message_id=number)
    private_reaction(transport, notice['forwarded_message_id'])
    core.collect()
    deliveries = [d for d in core.poll(sender, lease_seconds=10)['deliveries'] if d['kind'] == 'reaction']
    [delivery] = deliveries
    assert delivery['message_id'] == notice['forwarded_message_id'] and delivery['own_message'] is True


def test_owner_reaction_follows_reaction_wake(env):
    core, transport, _ = env
    sender = bind(core, transport)
    core.delivery_settings(sender, reaction_wake='none')
    notice = core.owner_notify(sender, 'Approve?', 'n1')
    private_reaction(transport, notice['message_id'])
    core.collect()
    assert core.poll(sender)['deliveries'] == []
    assert 'private_chat' not in private_reasons(core)


def test_other_private_reactions_and_membership_are_private_chat(env):
    core, transport, _ = env
    sender = bind(core, transport)
    notice = core.owner_notify(sender, 'Approve?', 'n1')
    # Someone else reacting in their own private chat with the bot, on the same id.
    private_reaction(transport, notice['message_id'], sender=202, chat=202)
    # The owner reacting to a message that is not a notice.
    private_reaction(transport, notice['message_id'] + 50)
    # The owner blocking the bot.
    number = max(u['update_id'] for u in transport.updates) + 1
    transport.updates.append({'update_id': number, 'my_chat_member': {
        'chat': {'id': OWNER, 'type': 'private'}, 'from': {'id': OWNER, 'is_bot': False},
        'date': 1000, 'old_chat_member': {'status': 'member', 'user': {'id': transport.bot_id, 'is_bot': True}},
        'new_chat_member': {'status': 'kicked', 'user': {'id': transport.bot_id, 'is_bot': True}}}})
    core.collect()
    assert core.poll(sender)['deliveries'] == []
    assert private_reasons(core)[:3] == ['private_chat'] * 3
    with sqlite3.connect(core.database) as db:
        assert db.execute("SELECT count(*) FROM unmatched WHERE reason='private_chat' AND chat_id IS NOT NULL").fetchone()[0] == 0
    # The group binding is untouched by the private membership change.
    assert core.poll(sender)['suspended'] is None


@pytest.mark.parametrize('exempt,announced', [('addressed,owner', True), ('owner', True),
                                              ('addressed', False), ('none', False)])
def test_owner_reaction_on_a_notice_wakes_at_once_while_batching(env, exempt, announced):
    from agents_over_telegram import push
    core, transport, clock = env
    sender = bind(core, transport)
    core.delivery_settings(sender, wake_interval=600, wake_exempt=exempt)
    push.ensure_tables(core)
    core.allow_sender(sender, 202)
    append(transport, 'first', sender=202)
    core.collect()
    assert push.claim_new(core, sender, 'watch')['new'] == 1  # starts the interval
    notice = core.owner_notify(sender, 'Approve the release?', 'n1')
    private_reaction(transport, notice['message_id'])
    core.collect()
    claimed = push.claim_new(core, sender, 'watch')
    assert (claimed is not None) == announced
    payload = core.poll(sender)['deliveries'][-1]
    assert payload['kind'] == 'reaction' and payload['owner_notice'] is True


def test_group_reactions_do_not_carry_the_owner_notice_mark(env):
    core, transport, _ = env
    sender = bind(core, transport)
    core.delivery_settings(sender, reaction_wake='all')
    number = append(transport, 'hello')
    core.collect()
    core.poll(sender)
    transport.updates.append({'update_id': number + 1, 'message_reaction': {
        'chat': {'id': GROUP, 'type': 'supergroup', 'title': 'Test group'}, 'message_id': number, 'date': 1000,
        'user': {'id': OWNER, 'is_bot': False, 'username': 'participant'},
        'old_reaction': [], 'new_reaction': [{'type': 'emoji', 'emoji': '👍'}]}})
    core.collect()
    [reaction] = [d for d in core.poll(sender, lease_seconds=10)['deliveries'] if d['kind'] == 'reaction']
    assert 'owner_notice' not in reaction


def private_reply(transport, text, reply_to, sender=OWNER, chat=OWNER, **fields):
    """The owner's private message; reply_to=None sends it without quoting anything."""
    number = max((u['update_id'] for u in transport.updates), default=0) + 1
    message = {'message_id': 500 + number, 'date': 1000, 'chat': {'id': chat, 'type': 'private', 'first_name': 'Owner'},
               'from': {'id': sender, 'is_bot': False, 'username': 'owner'}, **fields}
    if text is not None:
        message['text'] = text
    if reply_to is not None:
        message['reply_to_message'] = {'message_id': reply_to, 'chat': {'id': chat, 'type': 'private'},
                                       'from': {'id': transport.bot_id, 'is_bot': True, 'username': transport.username},
                                       'text': '[Agent: Alpha | Test group]\nApprove?'}
    transport.updates.append({'update_id': number, 'message': message})
    return message['message_id']


def test_owner_reply_to_a_notice_reaches_the_sending_agent(env):
    core, transport, clock = env
    sender = bind(core, transport)
    other = bind(core, transport, 'Beta', chat=-200)
    notice = core.owner_notify(sender, 'Approve the release?', 'n1')
    clock.advance()
    core.owner_notify(other, 'Unrelated question', 'n2')
    reply_id = private_reply(transport, 'Yes, ship it.', notice['message_id'])
    assert core.collect()['status'] == 'ok'
    [delivery] = core.poll(sender)['deliveries']
    assert delivery['kind'] == 'message' and delivery['text'] == 'Yes, ship it.'
    assert delivery['message_id'] == reply_id and delivery['reply_to'] == notice['message_id']
    assert delivery['chat_id'] == OWNER and delivery['thread_id'] is None
    assert delivery['sender']['id'] == OWNER
    assert delivery['owner_notice'] is True and delivery['addressed'] is True
    assert delivery['conversation_id'] == f'owner:{sender}:{notice["notify_id"]}'
    assert delivery['reply_to_message']['sender']['allowed'] is True
    assert core.poll(other)['deliveries'] == []
    assert 'private_chat' not in private_reasons(core)


def test_owner_reply_to_a_forward_reaches_the_sending_agent(env):
    core, transport, _ = env
    sender = bind(core, transport)
    number = append(transport, 'Seb, which option?')
    core.collect()
    core.poll(sender)
    notice = core.owner_notify(sender, 'Look at this.', 'n1', forward_message_id=number)
    private_reply(transport, 'Option B.', notice['forwarded_message_id'])
    core.collect()
    replies = [d for d in core.poll(sender, lease_seconds=10)['deliveries'] if d.get('owner_notice')]
    assert [d['text'] for d in replies] == ['Option B.']


def test_owner_reply_media_follows_the_media_setting(env):
    core, transport, _ = env
    sender = bind(core, transport)
    core.delivery_settings(sender, accept_media=False)
    notice = core.owner_notify(sender, 'Send me the log?', 'n1')
    private_reply(transport, None, notice['message_id'], caption='here',
                  document={'file_id': 'DOC1', 'file_unique_id': 'uDoc1', 'file_name': 'log.txt', 'file_size': 5})
    core.collect()
    [delivery] = core.poll(sender)['deliveries']
    assert delivery['text'] == 'here' and delivery['media_refused'] == ['document'] and 'attachments' not in delivery


def test_other_private_messages_stay_private_chat(env):
    core, transport, _ = env
    sender = bind(core, transport)
    notice = core.owner_notify(sender, 'Approve?', 'n1')
    # Not quoting anything.
    private_reply(transport, 'Yes', None)
    # Quoting a message that is not a notice.
    private_reply(transport, 'Yes', notice['message_id'] + 50)
    # Someone else replying in their own private chat, on the same message id.
    private_reply(transport, 'Yes', notice['message_id'], sender=202, chat=202)
    core.collect()
    assert core.poll(sender)['deliveries'] == []
    assert private_reasons(core)[:3] == ['private_chat'] * 3


def test_owner_notify_reply_to_answers_in_the_private_chat(env):
    core, transport, clock = env
    sender = bind(core, transport)
    other = bind(core, transport, 'Beta', chat=-200)
    notice = core.owner_notify(sender, 'Approve?', 'n1')
    reply_id = private_reply(transport, 'Which version?', notice['message_id'])
    core.collect()
    clock.advance()
    # Answer the owner's routed reply, and reply to the own notice.
    answer = core.owner_notify(sender, 'Version 2.', 'n2', reply_to=reply_id)
    assert answer['status'] == 'sent' and answer['reply_to'] == reply_id
    assert owner_messages(transport)[-1]['reply_to_message_id'] == reply_id
    clock.advance()
    assert core.owner_notify(sender, 'Also this.', 'n3', reply_to=notice['message_id'])['status'] == 'sent'
    # reply_to is part of the request.
    with pytest.raises(CoreError, match='idempotency_conflict'):
        core.owner_notify(sender, 'Version 2.', 'n2')
    # Another agent cannot reply to this agent's messages, and unknown ids fail.
    core.delivery_settings(other, owner_notify=True)
    for target in (reply_id, notice['message_id'], 9999):
        with pytest.raises(CoreError) as error:
            core.owner_notify(other, 'x', f'o{target}', reply_to=target)
        assert error.value.code == 'unknown_owner_reply'
    # The same 3-per-hour limit applies to replies.
    clock.advance()
    with pytest.raises(CoreError, match='owner_notify_limit'):
        core.owner_notify(sender, 'One more.', 'n4', reply_to=reply_id)
    assert _error_body('unknown_owner_reply')['error']['message'].startswith('reply_to is not')
    assert call_tool(core, sender, 'owner_notify', {'text': 'x', 'idempotency_key': 'k',
                                                    'reply_to': 0})['error']['code'] == 'invalid_arguments'


def test_message_send_does_not_reply_in_the_group_to_a_private_owner_reply(env):
    core, transport, _ = env
    sender = bind(core, transport)
    notice = core.owner_notify(sender, 'Approve?', 'n1')
    reply_id = private_reply(transport, 'Yes', notice['message_id'])
    core.collect()
    with pytest.raises(CoreError, match='unknown_reply'):
        core.send(sender, 'Thanks', 's1', reply_to=reply_id)


@pytest.mark.parametrize('exempt,announced', [('addressed,owner', True), ('addressed', True),
                                              ('owner', True), ('none', False)])
def test_owner_reply_wakes_at_once_while_batching(env, exempt, announced):
    from agents_over_telegram import push
    core, transport, _ = env
    sender = bind(core, transport)
    core.delivery_settings(sender, wake_interval=600, wake_exempt=exempt)
    push.ensure_tables(core)
    core.allow_sender(sender, 202)
    append(transport, 'first', sender=202)
    core.collect()
    assert push.claim_new(core, sender, 'watch')['new'] == 1  # starts the interval
    notice = core.owner_notify(sender, 'Approve?', 'n1')
    private_reply(transport, 'Yes', notice['message_id'])
    core.collect()
    assert (push.claim_new(core, sender, 'watch') is not None) == announced
