"""Read-only, scoped 24-hour history with stable authenticated pagination."""
import base64
import json
import sqlite3

import pytest

from agents_over_telegram.core import Core, CoreError
from agents_over_telegram.transport import FakeTransport


class Clock:
    def __init__(self):
        self.now = 1_700_000_000.0

    def __call__(self):
        return self.now

    def advance(self, seconds):
        self.now += seconds


@pytest.fixture
def env(tmp_path):
    clock = Clock()
    transport = FakeTransport()
    core = Core(tmp_path / 'state', transport, clock=clock)
    core.register_account(transport.bot_id, transport.username)
    return core, transport, clock


def incoming(transport, text, chat=-100, sender=101, **fields):
    number = max((u['update_id'] for u in transport.updates), default=0) + 1
    message = {'message_id': number, 'chat': {'id': chat, 'type': 'supergroup', 'title': 'History test'},
               'from': {'id': sender, 'is_bot': False}, 'text': text}
    message.update(fields)
    update = {'update_id': number, 'message': message}
    transport.updates.append(update)
    return update


def bind(core, transport, label='Alpha', chat=-100):
    client = core.create_client(label)['client_id']
    pairing = core.connection_begin(client)
    incoming(transport, pairing['command'], chat=chat)
    core.collect()
    core.confirm_pairing(pairing['pairing_id'])
    return client


def database_dump(core):
    with sqlite3.connect(core.database) as db:
        return '\n'.join(db.iterdump())


def decode_cursor(cursor):
    encoded = cursor.split('.')[0]
    return json.loads(base64.urlsafe_b64decode(encoded + '=' * (-len(encoded) % 4)))


def forge_cursor(cursor, change):
    value = decode_cursor(cursor)
    change(value)
    encoded = base64.urlsafe_b64encode(json.dumps(value, separators=(',', ':')).encode()).rstrip(b'=').decode()
    return encoded + '.' + cursor.split('.')[1]


def test_acknowledged_history_survives_restart_and_does_not_lease(env):
    core, transport, clock = env
    client = bind(core, transport)
    incoming(transport, 'remember after ack', date=int(clock()) - 30)
    core.collect()
    pending = core.poll(client)
    delivery_id = pending['deliveries'][0]['delivery_id']
    core.ack(client, [delivery_id], pending['lease_token'])
    restarted = Core(core.state_dir, transport, clock=clock, recover_sends=False)
    page = restarted.history(client)
    assert page['context_only'] is True
    assert page['next_cursor'] is None
    assert page['coverage']['kind'] == 'captured_only'
    assert 'does not backfill' in page['coverage']['description']
    assert page['coverage']['window_end'] - page['coverage']['window_start'] == 86400
    message = page['messages'][0]
    assert message['text'] == 'remember after ack'
    assert message['status'] == 'acknowledged'
    assert message['delivery_id'] == delivery_id
    assert message['event_at'] == clock() - 30
    assert message['captured_at'] == clock()
    assert message['context_only'] is True
    assert 'lease_token' not in message
    assert restarted.poll(client)['deliveries'] == []


def test_history_has_no_transport_database_or_queue_mutations(env):
    core, transport, clock = env
    client = bind(core, transport)
    incoming(transport, 'leased')
    incoming(transport, 'unleased')
    core.collect()
    lease = core.poll(client, limit=1)
    result = core.send(client, 'outgoing', 'outgoing')
    before = database_dump(core)
    calls = (list(transport.poll_offsets), list(transport.sent))

    class NoTransport:
        def __getattr__(self, name):
            raise AssertionError('history must not use transport')

    core.transport = NoTransport()
    page = core.history(client, limit=1)
    assert page['next_cursor']
    while page['next_cursor']:
        page = core.history(client, limit=1, cursor=page['next_cursor'])
    assert database_dump(core) == before
    assert (transport.poll_offsets, transport.sent) == calls
    # Existing lease remains valid, and history did not claim the other message.
    core.ack(client, [lease['deliveries'][0]['delivery_id']], lease['lease_token'])
    assert core.poll(client)['deliveries'][0]['text'] == 'unleased'
    assert core.delivery_status(client, result['outbox_id'])['status'] == 'sent'


def test_history_keeps_sending_state_with_recovery_disabled(env):
    core, transport, clock = env
    client = bind(core, transport)
    transport.outcomes = ['crash']
    with pytest.raises(SystemExit):
        core.send(client, 'interrupted', 'interrupted')
    before = database_dump(core)
    reader = Core(core.state_dir, transport, clock=clock, recover_sends=False)
    page = reader.history(client)
    assert page['messages'][0]['status'] == 'sending'
    assert database_dump(core) == before
    recovered = Core(core.state_dir, transport, clock=clock)
    assert recovered.history(client)['messages'][0]['status'] == 'unknown'


@pytest.mark.parametrize('offset,included', [(-90000, False), (-86401, False), (-86400, True), (-86399, True), (-82800, True), (0, True)])
def test_telegram_event_time_controls_24h_cutoff(env, offset, included):
    core, transport, clock = env
    client = bind(core, transport)
    incoming(transport, 'timestamped', date=int(clock()) + offset)
    core.collect()
    page = core.history(client)
    assert bool(page['messages']) is included
    if included:
        assert page['messages'][0]['event_at'] == clock() + offset
    # Historical filtering does not discard newly captured pending work.
    assert core.poll(client)['deliveries'][0]['text'] == 'timestamped'


@pytest.mark.parametrize('invalid', [None, False, True, 0, -10, '1700000000', 1_699_999_999.5, 1_700_000_001, 10**100])
def test_invalid_telegram_timestamp_falls_back_to_capture(env, invalid):
    core, transport, clock = env
    client = bind(core, transport)
    incoming(transport, 'fallback', date=invalid)
    core.collect()
    entry = core.history(client)['messages'][0]
    assert entry['event_at'] == entry['captured_at'] == clock()


def test_edit_uses_edit_time_not_old_message_creation(env):
    core, transport, clock = env
    client = bind(core, transport)
    original = incoming(transport, 'old original', date=int(clock()) - 86401)
    edited = incoming(transport, 'recent edit', date=int(clock()) - 86401, edit_date=int(clock()) - 12)
    edited['message']['message_id'] = original['message']['message_id']
    edited['edited_message'] = edited.pop('message')
    core.collect()
    messages = core.history(client)['messages']
    assert len(messages) == 1
    assert messages[0]['kind'] == 'edited'
    assert messages[0]['event_at'] == clock() - 12
    assert messages[0]['text'] == 'recent edit'


def test_missing_edit_timestamp_uses_capture_not_old_original_date(env):
    core, transport, clock = env
    client = bind(core, transport)
    edited = incoming(transport, 'edit fallback', date=int(clock()) - 86401)
    edited['edited_message'] = edited.pop('message')
    core.collect()
    assert core.history(client)['messages'][0]['event_at'] == clock()


def test_legacy_rows_migrate_and_use_captured_time(env):
    core, transport, clock = env
    client = bind(core, transport)
    incoming(transport, 'legacy event')
    core.collect()
    with sqlite3.connect(core.database) as db:
        db.execute('DROP INDEX inbox_history')
        db.execute('ALTER TABLE inbox DROP COLUMN event_at')
        db.execute('DROP TABLE history_keys')
    restarted = Core(core.state_dir, transport, clock=clock, recover_sends=False)
    message = restarted.history(client)['messages'][0]
    assert message['event_at'] == message['captured_at'] == clock()
    assert message['text'] == 'legacy event'
    clock.advance(86401)
    assert restarted.history(client)['messages'] == []
    assert restarted.poll(client)['deliveries'][0]['text'] == 'legacy event'


def test_local_outgoing_uses_created_time_and_includes_unknown_provenance(env):
    core, transport, clock = env
    client = bind(core, transport)
    transport.outcomes = ['unknown']
    sent = core.send(client, 'possibly published', 'unknown')
    message = core.history(client)['messages'][0]
    assert message['direction'] == message['origin'] == 'outgoing'
    assert message['status'] == 'unknown'
    assert message['outbox_id'] == sent['outbox_id']
    assert message['logical_sender'] == {'client_id': client, 'label': 'Alpha'}
    assert message['actual_bot']['id'] == transport.bot_id
    assert message['text'].endswith('possibly published')
    assert message['event_at'] == message['captured_at'] == clock()
    assert message['context_only'] is True
    clock.advance(86401)
    assert core.history(client)['messages'] == []
    assert len(transport.sent) == 1


def test_broker_history_has_distinct_scoped_provenance_and_current_publication(env):
    core, transport, clock = env
    alpha = bind(core, transport)
    beta = bind(core, transport, 'Beta', -200)
    core.grant_peer(alpha, beta)
    transport.outcomes = ['unknown']
    outgoing = core.send(alpha, 'shared work', 'peer', recipient=beta)
    alpha_messages = core.history(alpha)['messages']
    beta_messages = core.history(beta)['messages']
    assert len(alpha_messages) == len(beta_messages) == 1
    assert alpha_messages[0]['origin'] == 'outgoing'
    assert beta_messages[0]['origin'] == 'broker'
    assert beta_messages[0]['logical_sender']['client_id'] == alpha
    assert beta_messages[0]['actual_bot']['id'] == transport.bot_id
    assert beta_messages[0]['status'] == 'pending'
    assert beta_messages[0]['publication'] == {'outbox_id': outgoing['outbox_id'], 'status': 'unknown', 'message_id': None}
    core.resolve_unknown(outgoing['outbox_id'], 'accepted', message_id=99)
    assert core.history(beta)['messages'][0]['publication']['status'] == 'sent'
    assert core.poll(beta)['deliveries'][0]['text'] == 'shared work'


def test_snapshot_pagination_ties_new_inserts_and_restart(env):
    core, transport, clock = env
    client = bind(core, transport)
    for n in range(4):
        incoming(transport, f'in-{n}', date=int(clock()))
    core.collect()
    for n in range(3):
        core.send(client, f'out-{n}', f'out-{n}')
    original = core.history(client)['messages']
    assert len(original) == 7
    first = core.history(client, limit=2)
    frozen = first['coverage']['window_end']
    # Same timestamp and older-event-time inserts must both stay outside this snapshot.
    incoming(transport, 'late-same-time', date=int(clock()))
    incoming(transport, 'late-old-event', date=int(clock()) - 60)
    core.collect()
    core.send(client, 'late-outgoing', 'late-outgoing')
    restarted = Core(core.state_dir, transport, clock=clock, recover_sends=False)
    page, collected = first, list(first['messages'])
    while page['next_cursor']:
        clock.advance(1)
        page = restarted.history(client, limit=2, cursor=page['next_cursor'])
        assert page['coverage']['window_end'] == frozen
        assert page['coverage']['window_start'] == frozen - 86400
        collected.extend(page['messages'])
    identity = lambda item: item.get('delivery_id', item.get('outbox_id'))
    assert [identity(item) for item in collected] == [identity(item) for item in original]
    assert len({identity(item) for item in collected}) == 7
    assert len(restarted.history(client)['messages']) == 10


def test_pagination_is_sorted_by_event_time_across_streams(env):
    core, transport, clock = env
    client = bind(core, transport)
    incoming(transport, 'old', date=int(clock()) - 10)
    incoming(transport, 'recent', date=int(clock()) - 5)
    core.collect()
    core.send(client, 'newest', 'newest')
    messages = core.history(client)['messages']
    assert [message['event_at'] for message in messages] == [clock(), clock() - 5, clock() - 10]


def test_cursor_freezes_window_and_expires_in_fifteen_minutes(env):
    core, transport, clock = env
    client = bind(core, transport)
    incoming(transport, 'cutoff', date=int(clock()) - 86400)
    incoming(transport, 'recent', date=int(clock()))
    core.collect()
    first = core.history(client, limit=1)
    clock.advance(899)
    next_page = core.history(client, cursor=first['next_cursor'])
    assert next_page['messages'][0]['text'] == 'cutoff'
    assert next_page['coverage']['window_start'] == first['coverage']['window_start']
    assert len(core.history(client)['messages']) == 1  # Fresh window has moved forward.
    clock.advance(1)
    with pytest.raises(CoreError, match='history_cursor_expired'):
        core.history(client, cursor=first['next_cursor'])


def test_cursor_client_binding_and_signature_protect_all_pagination_fields(env):
    core, transport, clock = env
    alpha = bind(core, transport)
    beta = bind(core, transport, 'Beta', -200)
    incoming(transport, 'alpha-1')
    incoming(transport, 'alpha-2')
    incoming(transport, 'beta private', chat=-200)
    core.collect()
    cursor = core.history(alpha, limit=1)['next_cursor']
    with pytest.raises(CoreError, match='history_cursor_invalid'):
        core.history(beta, cursor=cursor)
    for change in [lambda v: v.update(client=beta), lambda v: v.update(at=v['at'] - 86400),
                   lambda v: v.update(at=v['at'] + 3600), lambda v: v.update(inbox_max=9999),
                   lambda v: v.update(outbox_max=9999), lambda v: v.update(last=[0, 0, 1])]:
        with pytest.raises(CoreError, match='history_cursor_invalid'):
            core.history(alpha, cursor=forge_cursor(cursor, change))
    for malformed in ['not-a-cursor', cursor + '.', cursor[:-3], 'FAKE_SECRET_TOKEN', 'a' * 2049, '', 123, False]:
        with pytest.raises(CoreError, match='history_cursor_invalid') as caught:
            core.history(alpha, cursor=malformed)
        assert 'FAKE_SECRET_TOKEN' not in str(caught.value)
    assert [message['text'] for message in core.history(beta)['messages']] == ['beta private']


def test_cursor_cannot_be_replayed_in_another_state_root(env, tmp_path):
    core, transport, clock = env
    client = bind(core, transport)
    incoming(transport, 'one')
    incoming(transport, 'two')
    core.collect()
    cursor = core.history(client, limit=1)['next_cursor']
    other = Core(tmp_path / 'other', transport, clock=clock)
    other.register_account(transport.bot_id, transport.username)
    other_client = bind(other, transport, 'Other', -200)
    with pytest.raises(CoreError, match='history_cursor_invalid'):
        other.history(other_client, cursor=cursor)


def test_paused_and_suspended_readable_but_unbound_and_revoked_forbidden(env):
    core, transport, clock = env
    client = bind(core, transport)
    incoming(transport, 'one')
    incoming(transport, 'two')
    core.collect()
    cursor = core.history(client, limit=1)['next_cursor']
    core.pause(client)
    assert len(core.history(client)['messages']) == 2
    incoming(transport, 'service', forum_topic_closed={})
    core.collect()
    assert len(core.history(client)['messages']) == 2
    unbound = core.create_client('Unbound')['client_id']
    with pytest.raises(CoreError, match='not_connected'):
        core.history(unbound)
    core.disconnect(client)
    with pytest.raises(CoreError, match='client_revoked'):
        core.history(client, cursor=cursor)
    replacement = bind(core, transport, 'Replacement')
    assert core.history(replacement)['messages'] == []


def test_prebinding_and_unapproved_messages_are_not_history_backfill(env):
    core, transport, clock = env
    incoming(transport, 'before pairing')
    client = bind(core, transport)
    incoming(transport, 'forged sender', sender=999)
    incoming(transport, 'unconfigured destination', chat=-999)
    incoming(transport, 'captured and authorized')
    core.collect()
    assert [m['text'] for m in core.history(client)['messages']] == ['captured and authorized']
    assert 'before pairing' not in database_dump(core)


@pytest.mark.parametrize('limit', [0, -1, 51, 1000, True, False, 1.0, None, '20'])
def test_invalid_history_limit(env, limit):
    core, transport, clock = env
    client = bind(core, transport)
    with pytest.raises(CoreError, match='invalid_history_options'):
        core.history(client, limit=limit)


def test_empty_history_and_fifty_entry_bound(env):
    core, transport, clock = env
    client = bind(core, transport)
    assert core.history(client)['messages'] == []
    assert core.history(client)['next_cursor'] is None
    for n in range(51):
        incoming(transport, str(n))
    core.collect()
    first = core.history(client, limit=50)
    assert len(first['messages']) == 50
    assert first['next_cursor']
    second = core.history(client, limit=50, cursor=first['next_cursor'])
    assert len(second['messages']) == 1
    assert second['next_cursor'] is None


def test_offline_gap_coverage_survives_read_without_intake(env):
    core, transport, clock = env
    client = bind(core, transport)
    clock.advance(86401)
    before = list(transport.poll_offsets)
    assert core.history(client)['coverage']['possible_offline_gap'] is True
    assert transport.poll_offsets == before
