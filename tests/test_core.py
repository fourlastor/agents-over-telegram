"""Deterministic offline proof of routing, authorization and failure semantics."""
import json
import os
import sqlite3
import stat
import threading
from concurrent.futures import ThreadPoolExecutor

import pytest

from agents_over_telegram.core import Core, CoreError
from agents_over_telegram.transport import FakeTransport, TransportError, UnknownSend
from conftest import posix_only


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
        message['chat']['is_forum'] = True
        message['is_topic_message'] = True
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


PNG = b'\x89PNG\r\n\x1a\n' + b'fake png body'
JPEG = b'\xff\xd8\xff\xe0' + b'fake jpeg body'
WEBP = b'RIFF\x10\x00\x00\x00WEBP' + b'fake webp body'
GIF = b'GIF89a' + b'fake gif body'


def image_file(directory, data, name='image.bin'):
    path = directory / name
    path.write_bytes(data)
    return str(path)


def stored_images(core):
    directory = core.state_dir / 'outbox-images'
    return list(directory.iterdir()) if directory.exists() else []


def rows(core, sql, args=()):
    with sqlite3.connect(core.database) as db:
        return db.execute(sql, args).fetchall()


@posix_only
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


def test_reply_in_a_supergroup_without_topics_reaches_the_group_binding(env):
    # Telegram sets message_thread_id on every reply in a supergroup, even
    # without forum topics: it names the first message of the reply chain.
    core, transport, _ = env
    client, _ = bind(core, transport)
    append(transport, 'plain reply', message_thread_id=10,
           reply_to_message={'message_id': 10, 'chat': {'id': -100, 'type': 'supergroup'}})
    assert core.collect()['updates'] == 1
    assert [m['text'] for m in core.poll(client)['deliveries']] == ['plain reply']
    assert rows(core, 'SELECT count(*) FROM unmatched')[0][0] == 0


def test_pairing_by_reply_in_a_supergroup_without_topics_binds_the_whole_group(env):
    core, transport, _ = env
    credentials = core.create_client('Alpha')
    pairing = core.connection_begin(credentials['client_id'])
    append(transport, pairing['command'], message_thread_id=10)
    core.collect()
    core.confirm_pairing(pairing['pairing_id'])
    assert rows(core, 'SELECT thread_id FROM bindings')[0][0] == 0


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


def test_a_full_queue_keeps_the_overflow_as_context_and_intake_moves_on(env):
    core, transport, clock = env
    core.queue_limit = 1
    a, _ = bind(core, transport)
    b, _ = bind(core, transport, 'Beta', -200)
    append(transport, 'fill')
    core.collect()
    before = rows(core, 'SELECT cursor FROM account')[0][0]
    append(transport, 'b first', chat=-200)
    append(transport, 'a overflow')
    assert core.collect()['updates'] == 2
    # One agent's full queue no longer holds back the page for everyone.
    assert rows(core, 'SELECT cursor FROM account')[0][0] == before + 2
    assert core.connection_status(b)['pending_messages'] == 1
    assert core.connection_status(a)['pending_messages'] == 1
    # The overflow is not lost: history has it, marked as kept for a full queue.
    kept = [m for m in core.history(a, limit=50)['messages'] if m.get('text') == 'a overflow']
    assert len(kept) == 1 and kept[0]['context_reason'] == 'queue_full'
    assert core.activity()[-1]['queue_full'] == 1
    # Draining the queue lets new messages through again, without duplicates.
    page = core.poll(a)
    assert [d['text'] for d in page['deliveries']] == ['fill']
    core.ack(a, [page['deliveries'][0]['delivery_id']], page['lease_token'])
    append(transport, 'a after drain')
    core.collect()
    assert [d['text'] for d in core.poll(a)['deliveries']] == ['a after drain']


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


@pytest.mark.parametrize('notice', ['old_group', 'new_supergroup', 'both'])
def test_group_migration_moves_the_binding_to_the_new_chat(env, notice):
    core, transport, _ = env
    a, _ = bind(core, transport)
    b, _ = bind(core, transport, 'Beta', -200)
    append(transport, 'pending before the upgrade')
    if notice in ('old_group', 'both'):
        append(transport, 'service', migrate_to_chat_id=-1009)
    if notice in ('new_supergroup', 'both'):
        append(transport, 'service', chat=-1009, migrate_from_chat_id=-100)
    append(transport, 'after the upgrade', chat=-1009)
    core.collect()
    status = core.connection_status(a)
    assert status['suspended'] is None
    assert rows(core, 'SELECT chat_id FROM bindings WHERE client_id=?', (a,))[0][0] == -1009
    assert [m['text'] for m in core.poll(a)['deliveries']] == ['pending before the upgrade', 'after the upgrade']
    assert rows(core, 'SELECT chat_id FROM bindings WHERE client_id=?', (b,))[0][0] == -200
    assert rows(core, 'SELECT count(*) FROM unmatched')[0][0] == 0


def test_group_migration_moves_queued_sends_to_the_new_chat(env):
    core, transport, _ = env
    a, _ = bind(core, transport)
    with sqlite3.connect(core.database) as db:
        db.execute("INSERT INTO outbox(id,client_id,chat_id,thread_id,idempotency_key,request_hash,text,"
                   "conversation_id,status,created,updated,due) VALUES('out_q',?,-100,0,'k','h','queued text',"
                   "'conv','queued',0,0,0)", (a,))
        db.execute("INSERT INTO outbox(id,client_id,chat_id,thread_id,idempotency_key,request_hash,text,"
                   "conversation_id,status,created,updated,due) VALUES('out_s',?,-100,0,'k2','h','sent text',"
                   "'conv','sent',0,0,0)", (a,))
    append(transport, 'service', migrate_to_chat_id=-1009)
    core.collect()
    assert dict(rows(core, 'SELECT id,chat_id FROM outbox')) == {'out_q': -1009, 'out_s': -100}


def test_group_migration_drops_reply_links_and_rejects_queued_reactions(env):
    # The supergroup numbers its own messages; ids from the old group would
    # point at unrelated messages there.
    core, transport, _ = env
    a, _ = bind(core, transport)
    with sqlite3.connect(core.database) as db:
        db.execute("INSERT INTO outbox(id,client_id,chat_id,thread_id,idempotency_key,request_hash,text,reply_to,"
                   "conversation_id,status,created,updated,due) VALUES('out_r',?,-100,0,'k','h','a reply',5,"
                   "'conv','queued',0,0,0)", (a,))
        db.execute("INSERT INTO outbox(id,client_id,chat_id,thread_id,idempotency_key,request_hash,text,reply_to,"
                   "conversation_id,status,created,updated,due) VALUES('out_s',?,-100,0,'k2','h','sent reply',5,"
                   "'conv','sent',0,0,0)", (a,))
        for status in ('queued', 'sent'):
            db.execute("INSERT INTO reaction_outbox(id,client_id,chat_id,thread_id,message_id,emoji,idempotency_key,"
                       "request_hash,status,created,updated,due) VALUES(?,?,-100,0,5,'👍',?,'h',?,0,0,0)",
                       ('react_' + status, a, status, status))
    append(transport, 'service', migrate_to_chat_id=-1009)
    core.collect()
    assert rows(core, 'SELECT id,chat_id,reply_to FROM outbox ORDER BY id') == [('out_r', -1009, None), ('out_s', -100, 5)]
    assert rows(core, 'SELECT id,chat_id,status,error FROM reaction_outbox ORDER BY id') == [
        ('react_queued', -100, 'rejected', 'group_migrated'), ('react_sent', -100, 'sent', None)]


def test_group_migration_onto_a_bound_chat_suspends_instead(env):
    core, transport, _ = env
    a, _ = bind(core, transport)
    taken, _ = bind(core, transport, 'Taken', -1009)
    append(transport, 'service', migrate_to_chat_id=-1009)
    core.collect()
    assert core.connection_status(a)['suspended'] == 'group_migrated:-1009'
    assert rows(core, 'SELECT chat_id FROM bindings WHERE client_id=?', (a,))[0][0] == -100
    assert core.connection_status(taken)['suspended'] is None


def migrated(new_chat_id):
    # What Telegram answers when the bot writes to a group that became a supergroup.
    return TransportError(400, migrate_to_chat_id=new_chat_id)


def test_send_refused_by_a_migration_moves_the_binding_and_retries_in_the_new_chat(env):
    core, transport, clock = env
    a, _ = bind(core, transport)
    transport.outcomes = [migrated(-1009)]
    held = core.send(a, 'hello', 'key')
    assert (held['status'], held['error']) == ('queued', 'group_migrated')
    assert rows(core, 'SELECT chat_id FROM bindings WHERE client_id=?', (a,))[0][0] == -1009
    assert core.connection_status(a)['suspended'] is None
    clock.advance()
    core.flush()
    assert core.delivery_status(a, held['outbox_id'])['status'] == 'sent'
    assert [m['chat']['id'] for m in transport.sent] == [-1009]


def test_send_refused_by_a_migration_follows_a_binding_already_moved(env):
    core, transport, clock = env
    a, _ = bind(core, transport)
    with sqlite3.connect(core.database) as db:
        # A send that was in flight while the collector moved the binding.
        db.execute("INSERT INTO outbox(id,client_id,chat_id,thread_id,idempotency_key,request_hash,text,reply_to,"
                   "conversation_id,status,created,updated,due) VALUES('out_q',?,-100,0,'k','h','late text',5,"
                   "'conv','queued',0,0,0)", (a,))
        db.execute('UPDATE bindings SET chat_id=-1009 WHERE client_id=?', (a,))
    transport.outcomes = [migrated(-1009)]
    core.flush()
    assert rows(core, "SELECT status,error,chat_id,reply_to FROM outbox WHERE id='out_q'") == [('queued', 'group_migrated', -1009, None)]
    clock.advance()
    core.flush()
    assert [m['chat']['id'] for m in transport.sent] == [-1009]


def test_send_refused_by_a_migration_onto_a_bound_chat_suspends_and_holds_it(env):
    core, transport, clock = env
    a, _ = bind(core, transport)
    bind(core, transport, 'Taken', -1009)
    transport.outcomes = [migrated(-1009)]
    held = core.send(a, 'hello', 'key')
    assert core.connection_status(a)['suspended'] == 'group_migrated:-1009'
    assert rows(core, 'SELECT status,chat_id FROM outbox WHERE id=?', (held['outbox_id'],)) == [('queued', -100)]
    clock.advance()
    core.flush()
    assert transport.sent == []


def test_other_refusals_with_a_bad_migration_target_are_still_rejected(env):
    core, transport, clock = env
    a, _ = bind(core, transport)
    transport.outcomes = [migrated(-100)]
    assert core.send(a, 'hello', 'key')['status'] == 'rejected'
    assert rows(core, 'SELECT chat_id FROM bindings WHERE client_id=?', (a,))[0][0] == -100


def test_reaction_refused_by_a_migration_is_rejected_but_moves_the_binding(env):
    core, transport, clock = env
    a, _ = bind(core, transport)
    target = append(transport, 'react to me')['message']['message_id']
    core.collect()
    transport.outcomes = [migrated(-1009)]
    reaction = core.react(a, target, '👍', 'r1')
    assert (reaction['status'], reaction['error']) == ('rejected', 'group_migrated')
    assert rows(core, 'SELECT chat_id FROM bindings WHERE client_id=?', (a,))[0][0] == -1009
    assert transport.reactions == []


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


@pytest.mark.parametrize('data,send_as,extension', [(PNG, 'photo', 'png'), (JPEG, 'photo', 'jpg'), (WEBP, 'photo', 'webp')])
def test_image_send_publishes_photo_with_label_caption(env, tmp_path, data, send_as, extension):
    core, transport, clock = env
    a, _ = bind(core, transport, topic=7)
    result = core.send(a, 'the landed change', 'shot-1', image_path=image_file(tmp_path, data, f'image.{extension}'))
    assert result['status'] == 'sent'
    assert core.delivery_status(a, result['outbox_id'])['status'] == 'sent'
    message = transport.sent[0]
    assert message['chat']['id'] == -100
    assert message['message_thread_id'] == 7
    assert message['caption'] == '[Agent: Alpha | shared bot @aot_test_bot]\nthe landed change'
    assert message['media']['kind'] == send_as
    assert message['media']['data'] == data
    assert message['media']['filename'].endswith('.' + extension)
    assert message['media']['mime'] in ('image/png', 'image/jpeg', 'image/webp')
    assert stored_images(core) == []


def test_image_send_gif_as_document_and_content_hashed_idempotency(env, tmp_path):
    core, transport, clock = env
    a, _ = bind(core, transport)
    result = core.send(a, 'animation', 'gif-1', image_path=image_file(tmp_path, GIF, 'loop.bin'))
    assert result['status'] == 'sent'
    assert transport.sent[0]['media']['kind'] == 'document'
    assert transport.sent[0]['media']['filename'].endswith('.gif')
    assert transport.sent[0]['media']['mime'] == 'image/gif'
    repeated = core.send(a, 'animation', 'gif-1', image_path=image_file(tmp_path, GIF, 'same-bytes.bin'))
    assert repeated['outbox_id'] == result['outbox_id']
    assert len(transport.sent) == 1
    with pytest.raises(CoreError, match='idempotency_conflict'):
        core.send(a, 'animation', 'gif-1', image_path=image_file(tmp_path, GIF + b'x', 'changed.bin'))
    with pytest.raises(CoreError, match='idempotency_conflict'):
        core.send(a, 'animation', 'gif-1')
    with pytest.raises(CoreError, match='idempotency_conflict'):
        core.send(a, 'animation', 'gif-1', image_path=image_file(tmp_path, PNG, 'png.bin'))


@pytest.mark.parametrize('data', [b'', b'plain text, not an image', b'BM\x36\x00\x00\x00' + b'bmp body', b'\xff\xd8' + b'truncated magic'])
def test_image_rejects_disallowed_types_before_any_send(env, tmp_path, data):
    core, transport, clock = env
    a, _ = bind(core, transport)
    with pytest.raises(CoreError, match='unsupported_image_type'):
        core.send(a, 'bad type', 'bad-1', image_path=image_file(tmp_path, data))
    assert transport.sent == []
    assert stored_images(core) == []


def test_image_rejects_unusable_paths_and_directories(env, tmp_path):
    core, transport, clock = env
    a, _ = bind(core, transport)
    for bad in [str(tmp_path / 'missing.png'), 'relative.png', str(tmp_path), '']:
        with pytest.raises(CoreError, match='invalid_image'):
            core.send(a, 'bad path', 'path-' + bad[:8], image_path=bad)
    assert transport.sent == []
    assert stored_images(core) == []


def test_image_rejects_over_ten_megabytes(env, tmp_path):
    core, transport, clock = env
    a, _ = bind(core, transport)
    path = tmp_path / 'big.png'
    path.write_bytes(PNG + b'\x00' * (10 * 1024 * 1024 - len(PNG) + 1))
    with pytest.raises(CoreError, match='image_too_large'):
        core.send(a, 'too big', 'big-1', image_path=str(path))
    assert transport.sent == []
    assert stored_images(core) == []


def test_image_caption_limit_covers_the_whole_publication(env, tmp_path):
    core, transport, clock = env
    a, _ = bind(core, transport, label='A' * 64)
    header = f'[Agent: {"A" * 64} | shared bot @{transport.username}]\n'
    fits = core.send(a, 'x' * (1024 - len(header)), 'fit-1', image_path=image_file(tmp_path, PNG))
    assert fits['status'] == 'sent'
    with pytest.raises(CoreError, match='caption_too_long'):
        core.send(a, 'x' * (1025 - len(header)), 'over-1', image_path=image_file(tmp_path, PNG))
    with pytest.raises(CoreError, match='caption_too_long'):
        core.send(a, 'x' * 1025, 'over-2', image_path=image_file(tmp_path, PNG))
    assert len(transport.sent) == 1
    assert len(transport.sent[0]['caption']) == 1024


@posix_only
def test_image_retry_uses_stored_copy_and_deletes_it_when_sent(env, tmp_path):
    core, transport, clock = env
    a, _ = bind(core, transport)
    source = tmp_path / 'shot.png'
    source.write_bytes(PNG)
    transport.outcomes = ['rate_limit']
    result = core.send(a, 'later', 'later-1', image_path=str(source))
    assert result['status'] == 'queued'
    stored = stored_images(core)
    assert len(stored) == 1
    assert stored[0].name.startswith(result['outbox_id'])
    assert stored[0].stat().st_mode & 0o777 == 0o600
    assert stored[0].parent.stat().st_mode & 0o777 == 0o700
    source.unlink()  # The agent's file is gone; the durable copy must carry the retry.
    clock.advance(31)
    core.flush()
    assert core.delivery_status(a, result['outbox_id'])['status'] == 'sent'
    assert transport.sent[0]['media']['data'] == PNG
    assert stored_images(core) == []


def test_image_unknown_outcome_keeps_copy_for_operator_retry(env, tmp_path):
    core, transport, clock = env
    a, _ = bind(core, transport)
    transport.outcomes = ['unknown']
    result = core.send(a, 'uncertain', 'unk-1', image_path=image_file(tmp_path, PNG))
    assert result['status'] == 'unknown'
    assert len(stored_images(core)) == 1
    resolved = core.resolve_unknown(result['outbox_id'], 'retry')
    assert resolved['status'] == 'queued'
    clock.advance(2)
    core.flush()
    assert core.delivery_status(a, result['outbox_id'])['status'] == 'sent'
    assert transport.sent[0]['media']['data'] == PNG
    assert stored_images(core) == []


def test_image_cancel_resolution_drops_copy_and_rejected_sends_do_too(env, tmp_path):
    core, transport, clock = env
    a, _ = bind(core, transport)
    transport.outcomes = ['unknown']
    result = core.send(a, 'uncertain cancel', 'cancel-1', image_path=image_file(tmp_path, PNG))
    assert core.resolve_unknown(result['outbox_id'], 'cancel')['status'] == 'rejected'
    assert stored_images(core) == []
    clock.advance(2)
    transport.outcomes = ['reject']
    rejected = core.send(a, 'refused image', 'refuse-1', image_path=image_file(tmp_path, GIF))
    assert rejected['status'] == 'rejected'
    assert rejected['error'] == 'telegram_rejected'
    assert stored_images(core) == []


def test_image_send_respects_allowed_recipients_and_limits(env, tmp_path):
    core, transport, clock = env
    a, _ = bind(core, transport)
    b, _ = bind(core, transport, 'Beta', -200)
    with pytest.raises(CoreError, match='peer_not_authorized'):
        core.send(a, 'to peer', 'peer-1', recipient=b, image_path=image_file(tmp_path, PNG))
    assert stored_images(core) == []
    core.exchange_limits(conversation_send_limit=1, conversation_window_seconds=120, reason='tight test limit')
    assert core.send(a, 'first', 'first-1')['status'] == 'sent'
    with pytest.raises(CoreError, match='conversation_limit'):
        core.send(a, 'second', 'second-1', image_path=image_file(tmp_path, PNG))
    assert len(transport.sent) == 1
    assert stored_images(core) == []


def test_image_missing_retry_copy_is_rejected_without_a_network_call(env, tmp_path):
    core, transport, clock = env
    a, _ = bind(core, transport)
    transport.outcomes = ['rate_limit']
    result = core.send(a, 'queued image', 'lost-1', image_path=image_file(tmp_path, PNG))
    assert result['status'] == 'queued'
    for path in stored_images(core):
        path.unlink()  # Simulate a lost local copy while the record is queued.
    clock.advance(31)
    core.flush()
    status = core.delivery_status(a, result['outbox_id'])
    assert status['status'] == 'rejected'
    assert status['error'] == 'outbox_image_unavailable'
    assert transport.sent == []


def test_image_send_to_authorized_peer_publishes_photo_with_caption_only_delivery(env, tmp_path):
    core, transport, clock = env
    a, _ = bind(core, transport)
    b, _ = bind(core, transport, 'Beta', -200)
    core.grant_peer(a, b)
    result = core.send(a, 'peer screenshot', 'peer-shot-1', recipient=b, image_path=image_file(tmp_path, PNG))
    assert result['status'] == 'sent'
    message = transport.sent[0]
    assert message['chat']['id'] == -200
    assert message['media']['kind'] == 'photo'
    assert message['media']['data'] == PNG
    assert message['caption'].startswith('[Agent: Alpha | shared bot @aot_test_bot]')
    # The peer agent's broker delivery carries the caption text only: no image
    # reference it can fetch. Pinned until peer image forwarding is designed.
    delivery = core.poll(b)['deliveries'][0]
    assert delivery['origin'] == 'broker'
    assert delivery['text'] == 'peer screenshot'
    assert 'attachments' not in delivery
    assert delivery['publication']['status'] == 'sent'


@posix_only
def test_image_read_survives_a_fifo_swapped_in_after_stat(env, tmp_path, monkeypatch):
    core, transport, clock = env
    a, _ = bind(core, transport)
    target = tmp_path / 'race.png'
    target.write_bytes(PNG)
    real_open, real_stat = os.open, os.stat
    swapped = False

    def swap_for_fifo():
        # Swap the regular file for a FIFO once, the worst case at open time:
        # the lookup already saw a regular image, and the path changed under
        # it. One-shot so existence checks cannot re-enter the patched calls.
        nonlocal swapped
        if not swapped:
            swapped = True
            target.unlink()
            os.mkfifo(target)

    def racing_stat(path, *args, **kwargs):
        result = real_stat(path, *args, **kwargs)
        if os.fspath(path) == str(target):
            swap_for_fifo()
        return result

    def racing_open(path, flags, *args, **kwargs):
        if os.fspath(path) == str(target):
            swap_for_fifo()
        return real_open(path, flags, *args, **kwargs)

    monkeypatch.setattr(os, 'stat', racing_stat)
    monkeypatch.setattr(os, 'open', racing_open)
    errors = []

    def attempt():
        try:
            core.send(a, 'racy', 'race-1', image_path=str(target))
        except CoreError as error:
            errors.append(error.code)

    worker = threading.Thread(target=attempt)
    worker.start()
    worker.join(5)
    assert not worker.is_alive(), 'a FIFO swapped in before the read must not block the send'
    assert errors == ['invalid_image']
    assert transport.sent == []
    assert stored_images(core) == []
    assert stat.S_ISFIFO(real_stat(target).st_mode)  # the swap really happened


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


def full_queue_notices(transport, start=0):
    return [m for m in transport.sent[start:] if 'has reached its limit of' in m['text']]


def test_a_full_queue_is_reported_to_the_agent_and_at_most_daily_to_the_owner(env):
    core, transport, clock = env
    core.queue_limit = 1
    a, _ = bind(core, transport)
    append(transport, 'fill')
    core.collect()
    assert core.connection_status(a)['queue_full_kept'] == 0
    start = len(transport.sent)
    append(transport, 'over one')
    append(transport, 'over two')
    core.collect()
    # The collector itself tells the owner privately, once, without the agent running.
    [notice] = full_queue_notices(transport, start)
    assert notice['chat']['id'] == 101
    assert '"Alpha" has reached its limit of 1 unread messages' in notice['text'] and 'over one' not in notice['text']
    # The agent learns it from its own status and poll.
    status = core.connection_status(a)
    assert status['queue_full_kept'] == 2 and 'messages_history' in status['next_action']['message']
    page = core.poll(a)
    assert page['queue_full_kept']['count'] == 2
    # Reading them in history clears the count, so the poll note goes away.
    assert {m['text'] for m in core.history(a, limit=50)['messages']} >= {'over one', 'over two'}
    assert core.connection_status(a)['queue_full_kept'] == 0
    assert 'queue_full_kept' not in core.poll(a)
    # Within a day, a further overflow does not notify the owner again.
    clock.advance()
    append(transport, 'over three')
    core.collect()
    assert len(full_queue_notices(transport, start)) == 1
    # Only overflow after the last read counts.
    assert core.connection_status(a)['queue_full_kept'] == 1
    clock.advance(core.QUEUE_FULL_NOTICE_INTERVAL + 1)
    append(transport, 'over four')
    core.collect()
    assert len(full_queue_notices(transport, start)) == 2
    # "over three" has left the history window by now; "over four" counts.
    assert core.connection_status(a)['queue_full_kept'] == 1


def test_reading_one_page_of_overflow_clears_only_that_page(env):
    core, transport, clock = env
    core.queue_limit = 1
    a, _ = bind(core, transport)
    for text in ('fill', 'over one', 'over two', 'over three'):
        append(transport, text)
    core.collect()
    assert core.connection_status(a)['queue_full_kept'] == 3
    # History is newest first: one page of one shows only the newest overflow.
    page = core.history(a, limit=1)
    assert [m['text'] for m in page['messages']] == ['over three']
    assert core.connection_status(a)['queue_full_kept'] == 2
    # Reading the same row again changes nothing; the next page reads the next one.
    core.history(a, limit=1)
    assert core.connection_status(a)['queue_full_kept'] == 2
    core.history(a, limit=1, cursor=page['next_cursor'])
    assert core.connection_status(a)['queue_full_kept'] == 1


def test_a_history_page_without_overflow_does_not_clear_the_count(env):
    core, transport, clock = env
    core.queue_limit = 1
    a, _ = bind(core, transport)
    append(transport, 'fill')
    append(transport, 'over')
    core.collect()
    for number in range(3):
        core.send(a, f'reply {number}', f'reply-{number}')
    clock.advance()
    # The newest page holds only the agent's own replies, not the overflow.
    assert [m['direction'] for m in core.history(a, limit=3)['messages']] == ['outgoing'] * 3
    assert core.connection_status(a)['queue_full_kept'] == 1


def test_a_poll_without_overflow_is_unchanged(env):
    core, transport, clock = env
    a, _ = bind(core, transport)
    append(transport, 'hello')
    core.collect()
    assert 'queue_full_kept' not in core.poll(a)


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


def test_conversation_limit_is_rolling_through_broker_and_human_does_not_reset_it(env):
    core, transport, clock = env
    a, _ = bind(core, transport)
    b, _ = bind(core, transport, 'Beta', -200)
    core.grant_peer(a, b)
    core.grant_peer(b, a)
    current, target = a, b
    conversation = None
    first = clock()
    for n in range(core.CONVERSATION_SEND_LIMIT):
        result = core.send(current, f'hop{n}', f'hop{n}', recipient=target, conversation_id=conversation)
        conversation = result['conversation_id']
        current, target = target, current
        clock.advance(2)
    with pytest.raises(CoreError, match='conversation_limit') as blocked:
        core.send(current, 'over', 'over', recipient=target, conversation_id=conversation)
    assert blocked.value.retry_at == first + core.CONVERSATION_WINDOW
    append(transport, 'unrelated human', chat=-100)
    core.collect()
    with pytest.raises(CoreError, match='conversation_limit'):
        core.send(a, 'old chain still blocked', 'old-chain', recipient=b, conversation_id=conversation)
    result = core.send(a, 'human starts new exchange', 'new-chain', recipient=b)
    assert result['conversation_id'] != conversation
    with pytest.raises(CoreError, match='unknown_conversation'):
        core.send(a, 'invent reset', 'fake-chain', conversation_id='invented')
    clock.now = blocked.value.retry_at
    reopened = core.send(a, 'window has room', 'reopened', recipient=b, conversation_id=conversation)
    assert reopened['conversation_id'] == conversation


def test_cross_bot_loop_limit_and_configured_command(env):
    core, transport, clock = env
    a, _ = bind(core, transport)
    core.allow_sender(a, 202, is_bot=True)
    core.configure_peer_command(a, 'peer_test_bot', 'coordinate')
    conversation = None
    for n in range(core.CONVERSATION_SEND_LIMIT):
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
        core.send(a, 'too many', 'peer-over', conversation_id=conversation)


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


@posix_only
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


def test_production_commits_wait_for_the_disk(env, monkeypatch):
    from conftest import REAL_SQLITE_SYNCHRONOUS
    core, transport, clock = env
    assert REAL_SQLITE_SYNCHRONOUS == 'FULL'
    monkeypatch.setattr(core, 'SQLITE_SYNCHRONOUS', REAL_SQLITE_SYNCHRONOUS)
    with core._db() as db:
        # 2 is FULL.
        assert db.execute('PRAGMA synchronous').fetchone()[0] == 2
