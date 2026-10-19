"""Deterministic offline proof of routing, authorization and failure semantics."""
import json
import os
import sqlite3
import threading
from concurrent.futures import ThreadPoolExecutor

import pytest

from agents_over_telegram.core import Core, CoreError
from agents_over_telegram.transport import FakeTransport, TransportError, UnknownSend


class Clock:
    def __init__(self):
        self.now = 1000.0

    def __call__(self):
        return self.now

    def advance(self, seconds=2):
        self.now += seconds


def update(number, text, chat=-100, topic=None, sender=101, bot=False, **fields):
    message = {'message_id': number, 'chat': {'id': chat, 'type': 'supergroup', 'title': 'Test group'},
               'from': {'id': sender, 'is_bot': bot, 'username': 'participant'}, 'text': text}
    if topic is not None:
        message['message_thread_id'] = topic
    message.update(fields)
    return {'update_id': number, 'message': message}


@pytest.fixture
def env(tmp_path):
    clock = Clock()
    transport = FakeTransport()
    core = Core(tmp_path / 'state', transport, clock=clock)
    core.register_account(transport.bot_id, transport.username)
    return core, transport, clock


def bind(core, transport, label='Alpha', chat=-100, topic=None, sender=101):
    credentials = core.create_client(label)
    client_id = credentials['client_id']
    pairing = core.connection_begin(client_id)
    number = max((u['update_id'] for u in transport.updates), default=0) + 1
    transport.updates.append(update(number, pairing['command'], chat, topic, sender))
    core.collect()
    core.confirm_pairing(pairing['pairing_id'])
    return client_id, credentials


def append(transport, text, **kwargs):
    number = max((u['update_id'] for u in transport.updates), default=0) + 1
    result = update(number, text, **kwargs)
    transport.updates.append(result)
    return result


def rows(core, sql, args=()):
    with sqlite3.connect(core.database) as db:
        return db.execute(sql, args).fetchall()


def test_account_once_many_clients_private_hashes(env):
    core, transport, clock = env
    a, credential_a = bind(core, transport)
    b, credential_b = bind(core, transport, 'Beta', -200)
    assert core.authenticate(credential_a['client_secret']) == a
    assert core.authenticate(credential_b['client_secret']) == b
    assert len(core.accounts_status(a)['accounts']) == 1
    assert core.accounts_status()['accounts'][0]['bot_id'] == transport.bot_id
    core.register_account(transport.bot_id, transport.username)
    with pytest.raises(CoreError, match='account_already_registered'):
        core.register_account(1234, 'other_bot')
    stored = rows(core, 'SELECT secret_hash FROM clients')
    assert all(credential_a['client_secret'] not in value[0] for value in stored)
    assert core.state_dir.stat().st_mode & 0o777 == 0o700
    assert core.database.stat().st_mode & 0o777 == 0o600


def test_pairing_requires_observed_human_and_local_confirmation(env):
    core, transport, clock = env
    client = core.create_client('Alpha')['client_id']
    pairing = core.connection_begin(client)
    with pytest.raises(CoreError, match='pairing_not_observed'):
        core.confirm_pairing(pairing['pairing_id'])
    append(transport, pairing['command'], sender=202, bot=True)
    append(transport, pairing['command'].replace('@aot_test_bot', '@other_bot'), sender=202)
    core.collect()
    assert core.pairing_status(pairing['pairing_id'])['sender_id'] is None
    append(transport, pairing['command'], sender=101, topic=7)
    append(transport, pairing['command'], sender=999, chat=-200)
    core.collect()
    observed = core.pairing_status(pairing['pairing_id'])
    assert (observed['sender_id'], observed['chat_id'], observed['thread_id']) == (101, -100, 7)
    assert core.connection_status(client)['connected'] is False
    assert core.confirm_pairing(pairing['pairing_id'])['destination']['owner_id'] == 101
    with pytest.raises(CoreError, match='pairing_unavailable'):
        core.confirm_pairing(pairing['pairing_id'])


def test_pairing_expiration_reissue_and_dedicated_destinations(env):
    core, transport, clock = env
    client = core.create_client('Alpha')['client_id']
    expired = core.connection_begin(client)
    clock.advance(core.PAIR_TTL)
    append(transport, expired['command'])
    core.collect()
    with pytest.raises(CoreError, match='pairing_unavailable'):
        core.confirm_pairing(expired['pairing_id'])
    old = core.connection_begin(client)
    current = core.connection_begin(client)
    append(transport, old['command'])
    append(transport, current['command'])
    core.collect()
    with pytest.raises(CoreError, match='pairing_unavailable'):
        core.confirm_pairing(old['pairing_id'])
    core.confirm_pairing(current['pairing_id'])
    other = core.create_client('Beta')['client_id']
    pairing = core.connection_begin(other)
    append(transport, pairing['command'])
    core.collect()
    with pytest.raises(CoreError, match='destination_already_bound'):
        core.confirm_pairing(pairing['pairing_id'])


def test_interleaved_topics_replay_restart_and_pagination(env):
    core, transport, clock = env
    a, _ = bind(core, transport, topic=1)
    b, _ = bind(core, transport, 'Beta', topic=2)
    append(transport, 'a1', topic=1)
    append(transport, 'b1', topic=2)
    append(transport, 'a2', topic=1)
    append(transport, 'wrong topic', topic=3)
    result = core.collect()
    assert result['updates'] == 4
    assert core.collect()['updates'] == 0
    restarted = Core(core.state_dir, transport, clock=clock)
    aa = restarted.poll(a, limit=1)
    assert [m['text'] for m in aa['deliveries']] == ['a1']
    assert [m['text'] for m in restarted.poll(a)['deliveries']] == ['a2']
    assert [m['text'] for m in restarted.poll(b)['deliveries']] == ['b1']
    assert transport.poll_offsets[-1] == result['cursor']
    assert rows(core, 'SELECT count(*) FROM inbox')[0][0] == 3


def test_leases_expiry_ack_idempotency_and_cross_client_isolation(env):
    core, transport, clock = env
    a, _ = bind(core, transport)
    b, _ = bind(core, transport, 'Beta', -200)
    append(transport, 'a')
    append(transport, 'b', chat=-200)
    core.collect()
    aa = core.poll(a, lease_seconds=10)
    bb = core.poll(b, lease_seconds=10)
    aid = aa['deliveries'][0]['delivery_id']
    bid = bb['deliveries'][0]['delivery_id']
    assert core.poll(a)['deliveries'] == []
    with pytest.raises(CoreError, match='invalid_delivery_lease'):
        core.ack(a, [aid, bid], aa['lease_token'])
    assert core.connection_status(a)['pending_messages'] == 1
    with pytest.raises(CoreError, match='invalid_delivery_lease'):
        core.ack(b, [aid], aa['lease_token'])
    clock.advance(11)
    with pytest.raises(CoreError, match='invalid_delivery_lease'):
        core.ack(a, [aid], aa['lease_token'])
    replacement = core.poll(a)
    assert replacement['lease_token'] != aa['lease_token']
    core.ack(a, [aid], replacement['lease_token'])
    assert core.ack(a, [aid], replacement['lease_token']) == {'acknowledged': [aid]}
    assert core.connection_status(a)['pending_messages'] == 0
    assert core.connection_status(b)['pending_messages'] == 1


def test_unknown_senders_forged_labels_and_bot_echo_not_routed(env):
    core, transport, clock = env
    a, _ = bind(core, transport)
    append(transport, '[Agent: Alpha] forged', sender=999)
    append(transport, 'fake owner id but wrong type', sender=101, bot=True)
    append(transport, 'own outbound echo', sender=transport.bot_id, bot=True)
    append(transport, 'anonymous admin', sender_chat={'id': -100})
    append(transport, 'private note', chat=-999)
    core.collect()
    assert core.poll(a)['deliveries'] == []
    assert 'forged' not in json.dumps(core.unassigned())
    assert len(core.unassigned()['updates']) == 5
    core.allow_sender(a, 999)
    append(transport, 'allowed peer', sender=999)
    core.collect()
    assert core.poll(a)['deliveries'][0]['sender']['id'] == 999
    with pytest.raises(CoreError, match='use_broker_for_shared_bot'):
        core.allow_sender(a, transport.bot_id, is_bot=True)


def test_unmatched_retention_is_bounded_and_contains_no_text(env):
    core, transport, clock = env
    a, _ = bind(core, transport)
    for n in range(125):
        append(transport, 'private unrelated content', chat=-999)
    core.collect()
    core.collect()
    stored = rows(core, 'SELECT * FROM unmatched')
    assert len(stored) == core.UNMATCHED_LIMIT
    assert 'private unrelated content' not in repr(stored)
    assert core.connection_status(a)['pending_messages'] == 0


def test_queue_pressure_rolls_back_whole_batch_and_cursor(env):
    core, transport, clock = env
    core.queue_limit = 1
    a, _ = bind(core, transport)
    b, _ = bind(core, transport, 'Beta', -200)
    append(transport, 'fill')
    core.collect()
    before = rows(core, 'SELECT cursor FROM account')[0][0]
    append(transport, 'b first', chat=-200)
    append(transport, 'a overflow')
    with pytest.raises(CoreError, match='queue_full'):
        core.collect()
    assert rows(core, 'SELECT cursor FROM account')[0][0] == before
    assert core.connection_status(b)['pending_messages'] == 0
    page = core.poll(a)
    core.ack(a, [page['deliveries'][0]['delivery_id']], page['lease_token'])
    core.collect()
    assert core.poll(a)['deliveries'][0]['text'] == 'a overflow'
    assert core.poll(b)['deliveries'][0]['text'] == 'b first'


def test_crash_during_route_rolls_back_and_replays(env, monkeypatch):
    core, transport, clock = env
    a, _ = bind(core, transport)
    append(transport, 'recover me')
    before = rows(core, 'SELECT cursor FROM account')[0][0]
    route = core._route_update

    def crash(db, update, account, now):
        route(db, update, account, now)
        raise SystemExit('simulated crash before commit')

    monkeypatch.setattr(core, '_route_update', crash)
    with pytest.raises(SystemExit):
        core.collect()
    assert rows(core, 'SELECT cursor FROM account')[0][0] == before
    assert core.connection_status(a)['pending_messages'] == 0
    restarted = Core(core.state_dir, transport, clock=clock)
    restarted.collect()
    assert restarted.poll(a)['deliveries'][0]['text'] == 'recover me'


def test_one_collector_lock_and_restart_while_sender_runs(env):
    core, transport, clock = env
    a, _ = bind(core, transport)
    entered, release = threading.Event(), threading.Event()
    original = transport.get_updates

    def blocking(**kwargs):
        entered.set()
        assert release.wait(5)
        return original(**kwargs)

    transport.get_updates = blocking
    other = Core(core.state_dir, transport, clock=clock)
    with ThreadPoolExecutor(max_workers=1) as executor:
        running = executor.submit(core.collect)
        assert entered.wait(5)
        assert other.collect()['status'] == 'busy'
        release.set()
        assert running.result(timeout=5)['status'] == 'ok'
    assert other.collect()['status'] == 'ok'


def test_concurrent_leases_do_not_overlap(env):
    core, transport, clock = env
    a, _ = bind(core, transport)
    for n in range(20):
        append(transport, str(n))
    core.collect()
    other = Core(core.state_dir, transport, clock=clock)
    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(c.poll, a, 10) for c in (core, other)]
        pages = [f.result() for f in futures]
    ids = [m['delivery_id'] for page in pages for m in page['deliveries']]
    assert len(ids) == len(set(ids)) == 20


def test_webhook_conflict_never_polls_or_deletes(env):
    core, transport, clock = env
    bind(core, transport)
    calls = list(transport.poll_offsets)
    transport.webhook = 'https://secret.example/FAKE_TOKEN'
    with pytest.raises(CoreError, match='webhook_conflict') as caught:
        core.collect()
    assert transport.poll_offsets == calls
    assert 'FAKE_TOKEN' not in str(caught.value)
    assert core.accounts_status()['accounts'][0]['intake_error'] == 'webhook_conflict'


def test_pause_keeps_intake_blocks_claim_send_then_resume(env):
    core, transport, clock = env
    a, _ = bind(core, transport)
    core.pause(a)
    append(transport, 'pending while paused')
    core.collect()
    assert core.poll(a)['paused'] is True
    assert core.connection_status(a)['pending_messages'] == 1
    with pytest.raises(CoreError, match='connection_paused'):
        core.send(a, 'not sent', 'p1')
    core.pause(a, False)
    assert core.poll(a)['deliveries'][0]['text'] == 'pending while paused'
    append(transport, '/pause@aot_test_bot', sender=999)
    core.collect()
    assert core.connection_status(a)['paused'] is False
    append(transport, '/pause@aot_test_bot')
    core.collect()
    assert core.connection_status(a)['paused'] is True


def test_disconnect_revokes_secret_preserves_history_other_agent_works(env):
    core, transport, clock = env
    a, credential = bind(core, transport)
    b, _ = bind(core, transport, 'Beta', -200)
    append(transport, 'persist')
    core.collect()
    core.disconnect(a)
    for operation in (lambda: core.authenticate(credential['client_secret']), lambda: core.poll(a), lambda: core.send(a, 'no', 'x'), lambda: core.connection_status(a)):
        with pytest.raises(CoreError):
            operation()
    assert rows(core, 'SELECT count(*) FROM inbox WHERE client_id=?', (a,))[0][0] == 1
    append(transport, 'for b', chat=-200)
    core.collect()
    assert core.poll(b)['deliveries'][0]['text'] == 'for b'
    replacement, _ = bind(core, transport, 'Replacement')
    assert core.poll(replacement)['deliveries'] == []


@pytest.mark.parametrize('event,reason', [
    ({'migrate_to_chat_id': -999}, 'group_migrated:-999'),
    ({'forum_topic_closed': {}}, 'topic_changed'),
    ({'forum_topic_edited': {'name': 'new'}}, 'topic_changed'),
])
def test_migration_topic_change_suspend_preserve_pending(env, event, reason):
    core, transport, clock = env
    a, _ = bind(core, transport)
    append(transport, 'pending')
    append(transport, 'service', **event)
    core.collect()
    assert core.connection_status(a)['suspended'] == reason
    assert core.connection_status(a)['pending_messages'] == 1
    assert core.poll(a)['deliveries'] == []
    with pytest.raises(CoreError, match='connection_suspended'):
        core.pause(a, False)


def test_bot_removal_suspends_only_affected_group(env):
    core, transport, clock = env
    a, _ = bind(core, transport)
    b, _ = bind(core, transport, 'Beta', -200)
    transport.updates.append({'update_id': 100, 'my_chat_member': {'chat': {'id': -100}, 'new_chat_member': {'user': {'id': transport.bot_id}, 'status': 'kicked'}}})
    core.collect()
    assert core.connection_status(a)['suspended'] == 'bot_removed_or_restricted'
    assert core.connection_status(b)['suspended'] is None


def test_edits_have_explicit_kind_and_distinct_delivery(env):
    core, transport, clock = env
    a, _ = bind(core, transport)
    original = append(transport, 'original')
    edited = update(original['update_id'] + 1, 'edited')
    edited['message']['message_id'] = original['message']['message_id']
    edited['edited_message'] = edited.pop('message')
    transport.updates.append(edited)
    core.collect()
    page = core.poll(a)
    assert [m['kind'] for m in page['deliveries']] == ['message', 'edited']
    assert len({m['delivery_id'] for m in page['deliveries']}) == 2
    assert len({m['conversation_id'] for m in page['deliveries']}) == 1


def test_sends_label_shared_identity_idempotency_and_destination(env):
    core, transport, clock = env
    a, _ = bind(core, transport, topic=7)
    sent = core.send(a, 'hello', 'key')
    assert sent['status'] == 'sent'
    assert transport.sent[0]['chat']['id'] == -100
    assert transport.sent[0]['message_thread_id'] == 7
    assert transport.sent[0]['text'] == '[Agent: Alpha | shared bot @aot_test_bot]\nhello'
    assert core.send(a, 'hello', 'key')['outbox_id'] == sent['outbox_id']
    assert len(transport.sent) == 1
    with pytest.raises(CoreError, match='idempotency_conflict'):
        core.send(a, 'changed', 'key')
    b, _ = bind(core, transport, 'Beta', -200)
    with pytest.raises(CoreError, match='unknown_delivery'):
        core.delivery_status(b, sent['outbox_id'])


@pytest.mark.parametrize('outcome,status,error', [('reject', 'rejected', 'telegram_rejected'), ('unknown', 'unknown', 'uncertain_send'), (TransportError('FAKE_SECRET'), 'unknown', 'transport_failure'), (RuntimeError('FAKE_SECRET'), 'unknown', 'transport_failure')])
def test_rejected_and_unknown_never_automatically_resend(env, outcome, status, error):
    core, transport, clock = env
    a, _ = bind(core, transport)
    transport.outcomes = [outcome]
    result = core.send(a, 'message', 'key')
    assert result['status'] == status
    assert result['error'] == error
    calls = len(transport.sent)
    clock.advance(100)
    core.flush()
    assert core.send(a, 'message', 'key')['status'] == status
    assert len(transport.sent) == calls
    assert 'FAKE_SECRET' not in json.dumps(result)


def test_429_retries_only_when_due_and_gates_whole_bot(env):
    core, transport, clock = env
    a, _ = bind(core, transport)
    b, _ = bind(core, transport, 'Beta', -200)
    transport.outcomes = ['rate_limit']
    result = core.send(a, 'later', 'rate')
    assert result['status'] == 'queued'
    assert result['retry_at'] == clock() + 30
    other = core.send(b, 'also later', 'rate')
    assert other['status'] == 'queued'
    assert transport.sent == []
    clock.advance(31)
    core.flush()
    assert core.delivery_status(a, result['outbox_id'])['status'] == 'sent'
    clock.advance(2)
    core.flush()
    assert core.delivery_status(b, other['outbox_id'])['status'] == 'sent'
    assert len(transport.sent) == 2


def test_crash_after_acceptance_recovers_unknown_on_startup(env):
    core, transport, clock = env
    a, _ = bind(core, transport)
    transport.outcomes = ['crash']
    with pytest.raises(SystemExit):
        core.send(a, 'accepted before crash', 'crash')
    assert rows(core, 'SELECT status FROM outbox')[0][0] == 'sending'
    restarted = Core(core.state_dir, transport, clock=clock)
    result = restarted.send(a, 'accepted before crash', 'crash')
    assert result['status'] == 'unknown'
    assert result['error'] == 'interrupted_send'
    assert len(transport.sent) == 1
    clock.advance(100)
    restarted.flush()
    assert len(transport.sent) == 1


def test_live_send_is_not_marked_unknown_by_concurrent_startup(env):
    core, transport, clock = env
    a, _ = bind(core, transport)
    entered, release = threading.Event(), threading.Event()
    original = transport.send_message

    def blocking(**kwargs):
        entered.set()
        assert release.wait(5)
        return original(**kwargs)

    transport.send_message = blocking
    with ThreadPoolExecutor(max_workers=1) as pool:
        pending = pool.submit(core.send, a, 'running', 'running')
        assert entered.wait(5)
        other = Core(core.state_dir, transport, clock=clock)
        assert rows(other, 'SELECT status FROM outbox')[0][0] == 'sending'
        release.set()
        assert pending.result(timeout=5)['status'] == 'sent'


@pytest.mark.parametrize('resolution,status,count', [('accepted', 'sent', 1), ('cancel', 'rejected', 1), ('retry', 'sent', 2)])
def test_unknown_requires_operator_resolution(env, resolution, status, count):
    core, transport, clock = env
    a, _ = bind(core, transport)
    transport.outcomes = ['unknown']
    result = core.send(a, 'unknown', 'key')
    with pytest.raises(CoreError, match='message_id_required'):
        core.resolve_unknown(result['outbox_id'], 'accepted')
    core.resolve_unknown(result['outbox_id'], resolution, message_id=1 if resolution == 'accepted' else None)
    clock.advance(2)
    core.flush()
    assert core.delivery_status(a, result['outbox_id'])['status'] == status
    assert len(transport.sent) == count


def test_same_bot_broker_needs_grant_no_echo_and_no_duplicate_delivery(env):
    core, transport, clock = env
    a, _ = bind(core, transport)
    b, _ = bind(core, transport, 'Beta', -200)
    with pytest.raises(CoreError, match='peer_not_authorized'):
        core.send(a, 'work', 'broker', recipient=b)
    core.grant_peer(a, b)
    transport.outcomes = ['unknown']
    result = core.send(a, 'work', 'broker', recipient=b)
    assert result['status'] == 'unknown'
    assert transport.sent[0]['chat']['id'] == -200
    assert len(transport.updates) == 2  # Pairing only; no outgoing Telegram echo.
    page = core.poll(b)
    item = page['deliveries'][0]
    assert item['origin'] == 'broker'
    assert item['logical_sender'] == {'client_id': a, 'label': 'Alpha'}
    assert item['actual_bot']['id'] == transport.bot_id
    assert item['publication']['status'] == 'unknown'
    assert core.poll(a)['deliveries'] == []
    core.send(a, 'work', 'broker', recipient=b)
    assert rows(core, 'SELECT count(*) FROM inbox WHERE client_id=?', (b,))[0][0] == 1
    with pytest.raises(CoreError, match='peer_not_authorized'):
        core.send(b, 'reply', 'reply', recipient=a, conversation_id=item['conversation_id'])


def test_broker_capacity_failure_rolls_back_outbox(env):
    core, transport, clock = env
    core.queue_limit = 1
    a, _ = bind(core, transport)
    b, _ = bind(core, transport, 'Beta', -200)
    core.grant_peer(a, b)
    core.send(a, 'one', 'one', recipient=b)
    with pytest.raises(CoreError, match='queue_full'):
        core.send(a, 'two', 'two', recipient=b)
    assert rows(core, 'SELECT count(*) FROM outbox')[0][0] == 1
    assert len(transport.sent) == 1


def test_chain_limit_persists_through_broker_and_human_does_not_reset_it(env):
    core, transport, clock = env
    a, _ = bind(core, transport)
    b, _ = bind(core, transport, 'Beta', -200)
    core.grant_peer(a, b)
    core.grant_peer(b, a)
    current, target = a, b
    conversation = None
    for n in range(core.MAX_CHAIN):
        result = core.send(current, f'hop{n}', f'hop{n}', recipient=target, conversation_id=conversation)
        conversation = result['conversation_id']
        current, target = target, current
        clock.advance(2)
    with pytest.raises(CoreError, match='conversation_limit'):
        core.send(current, 'ninth', 'ninth', recipient=target, conversation_id=conversation)
    append(transport, 'unrelated human', chat=-100)
    core.collect()
    with pytest.raises(CoreError, match='conversation_limit'):
        core.send(a, 'old chain still blocked', 'old-chain', recipient=b, conversation_id=conversation)
    result = core.send(a, 'human starts new exchange', 'new-chain', recipient=b)
    assert result['conversation_id'] != conversation
    with pytest.raises(CoreError, match='unknown_conversation'):
        core.send(a, 'invent reset', 'fake-chain', conversation_id='invented')


def test_cross_bot_loop_limit_and_configured_command(env):
    core, transport, clock = env
    a, _ = bind(core, transport)
    core.allow_sender(a, 202, is_bot=True)
    core.configure_peer_command(a, 'peer_test_bot', 'coordinate')
    conversation = None
    for n in range(core.MAX_CHAIN):
        append(transport, f'peer message {n}', sender=202, bot=True)
        core.collect()
        page = core.poll(a)
        current = page['deliveries'][-1]['conversation_id']
        if conversation:
            assert current == conversation
        conversation = current
        core.send(a, f'reply {n}', f'peer{n}', conversation_id=conversation)
        clock.advance(2)
    assert transport.sent[0]['text'].startswith('/coordinate@peer_test_bot\n[Agent: Alpha')
    with pytest.raises(CoreError, match='conversation_limit'):
        core.send(a, 'too many', 'peer9', conversation_id=conversation)


def test_replies_must_belong_to_destination_and_inherit_conversation(env):
    core, transport, clock = env
    a, _ = bind(core, transport)
    incoming = append(transport, 'human asks')
    core.collect()
    page = core.poll(a)
    result = core.send(a, 'answer', 'answer', reply_to=incoming['message']['message_id'])
    assert result['conversation_id'] == page['deliveries'][0]['conversation_id']
    with pytest.raises(CoreError, match='unknown_reply'):
        core.send(a, 'bad reply', 'bad-reply', reply_to=999999)
    append(transport, 'followup', reply_to_message={'message_id': result['message_id']})
    core.collect()
    assert core.poll(a)['deliveries'][0]['conversation_id'] == result['conversation_id']


def test_invalid_state_permissions_and_symlink_rejected(tmp_path):
    insecure = tmp_path / 'insecure'
    insecure.mkdir(mode=0o755)
    with pytest.raises(CoreError, match='insecure_state_directory'):
        Core(insecure, FakeTransport())
    secure = tmp_path / 'secure'
    secure.mkdir(mode=0o700)
    linked = tmp_path / 'linked'
    linked.symlink_to(secure, target_is_directory=True)
    with pytest.raises(CoreError, match='insecure_state_directory'):
        Core(linked, FakeTransport())
    target = tmp_path / 'other-db'
    target.write_bytes(b'')
    (secure / 'state.sqlite3').symlink_to(target)
    with pytest.raises(CoreError, match='insecure_state_file'):
        Core(secure, FakeTransport())


def test_malformed_updates_fail_without_cursor_advance(env):
    core, transport, clock = env
    a, _ = bind(core, transport)
    cursor = rows(core, 'SELECT cursor FROM account')[0][0]
    transport.get_updates = lambda **kwargs: [{'update_id': 'FAKE_SECRET'}, {'update_id': 3}]
    with pytest.raises(CoreError, match='invalid_transport_response') as caught:
        core.collect()
    assert rows(core, 'SELECT cursor FROM account')[0][0] == cursor
    assert 'FAKE_SECRET' not in str(caught.value)
    transport.get_updates = lambda **kwargs: [{'update_id': 100, 'message': {'chat': [], 'from': []}}]
    assert core.collect()['cursor'] == 101


def test_offline_gap_visible_after_reconnect(env):
    core, transport, clock = env
    a, _ = bind(core, transport)
    clock.advance(86401)
    assert core.accounts_status(a)['accounts'][0]['possible_offline_gap'] is True
    core.collect()
    status = core.accounts_status(a)['accounts'][0]
    assert status['possible_offline_gap'] is True
    assert status['last_gap_at'] == clock()
