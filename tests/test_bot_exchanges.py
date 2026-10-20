"""Offline proof for opt-in external bot exchanges. No live state or credentials."""
from concurrent.futures import ThreadPoolExecutor
import sqlite3

import pytest

from agents_over_telegram.core import Core, CoreError
from agents_over_telegram.cli import parser
from agents_over_telegram.transport import FakeTransport
from test_core import Clock, append, bind, rows


@pytest.fixture
def setup_pair(tmp_path):
    clock, transport = Clock(), FakeTransport()
    core = Core(tmp_path / 'state', transport, clock=clock)
    core.register_account(transport.bot_id, transport.username)
    client, _ = bind(core, transport)
    core.allow_sender(client, 202, is_bot=True)
    return core, transport, clock, client


def receive(core, transport, client, **fields):
    sender = fields.pop('sender', 202)
    event = append(transport, 'independent request', sender=sender, bot=True, **fields)
    core.collect()
    conversation = rows(core, 'SELECT conversation_id FROM inbox WHERE client_id=? AND event_key=?',
                        (client, f'tg:{event["update_id"]}'))[0][0]
    return conversation, event


def send_n(core, clock, client, conversation, prefix, count=8):
    sent = []
    for n in range(count):
        sent.append(core.send(client, 'reply', f'{prefix}-{n}', conversation_id=conversation))
        clock.advance(2)
    return sent


def test_existing_state_keeps_legacy_policy_and_counters(setup_pair):
    core, transport, clock, client = setup_pair
    conv, _ = receive(core, transport, client)
    send_n(core, clock, client, conv, 'legacy')
    before = rows(core, 'SELECT * FROM conversations')
    before_inbox = rows(core, 'SELECT id,conversation_id,acknowledged FROM inbox')
    # Simulate the exact pre-migration schema without touching stored records.
    with sqlite3.connect(core.database) as db:
        db.execute('DROP TABLE bot_exchange_policies')
        db.execute('DROP TABLE bot_exchange_members')
    core = Core(core.state_dir, transport, clock=clock)
    assert not core.bot_exchange_policy(client, 202)['enabled']
    assert rows(core, 'SELECT * FROM conversations') == before
    assert rows(core, 'SELECT id,conversation_id,acknowledged FROM inbox') == before_inbox
    fresh, _ = receive(core, transport, client)
    assert fresh == conv
    with pytest.raises(CoreError, match='conversation_limit'):
        core.send(client, 'blocked', 'legacy-blocked', conversation_id=fresh)
    result = core.bot_exchange_policy(client, 202, enabled=True)
    assert result['reserved_in_window'] == 8
    assert rows(core, 'SELECT * FROM conversations') == before
    fresh, _ = receive(core, transport, client)
    assert fresh != conv
    assert core.send(client, 'new work', 'new-work', conversation_id=fresh)['status'] == 'sent'
    with pytest.raises(CoreError, match='conversation_limit'):
        core.send(client, 'old still blocked', 'old-still-blocked', conversation_id=conv)


def test_reply_lineage_never_expires_or_resets(setup_pair):
    core, transport, clock, client = setup_pair
    core.bot_exchange_policy(client, 202, enabled=True)
    conv, _ = receive(core, transport, client)
    sent = send_n(core, clock, client, conv, 'lineage')
    clock.advance(core.BOT_PAIR_WINDOW + 1)
    reply, _ = receive(core, transport, client, reply_to_message={'message_id': sent[-1]['message_id']})
    assert reply == conv
    with pytest.raises(CoreError, match='conversation_limit'):
        core.send(client, 'ninth', 'ninth', conversation_id=reply)
    fresh, _ = receive(core, transport, client)
    assert fresh != conv
    assert core.send(client, 'fresh', 'fresh', conversation_id=fresh)['status'] == 'sent'


@pytest.mark.parametrize('disable', [False, True])
def test_edits_reuse_original_root_even_after_disabling(setup_pair, disable):
    core, transport, clock, client = setup_pair
    core.bot_exchange_policy(client, 202, enabled=True)
    conv, original = receive(core, transport, client)
    send_n(core, clock, client, conv, 'edit')
    if disable:
        core.bot_exchange_policy(client, 202, enabled=False)
    event = {'update_id': original['update_id'] + 1, 'edited_message': dict(original['message'], text='edited')}
    transport.updates.append(event)
    core.collect()
    edited = rows(core, 'SELECT conversation_id FROM inbox WHERE event_key=?', (f'tg:{event["update_id"]}',))[0][0]
    assert edited == conv
    with pytest.raises(CoreError, match='conversation_limit'):
        core.send(client, 'edit cannot reopen', 'edit-cannot-reopen', conversation_id=edited)


def test_unknown_reply_and_unseen_edit_do_not_mint_roots(setup_pair):
    core, transport, clock, client = setup_pair
    core.bot_exchange_policy(client, 202, enabled=True)
    conv, _ = receive(core, transport, client, reply_to_message={'message_id': 99999})
    assert conv == f'bot:{client}:202'
    event = append(transport, 'unseen edit', sender=202, bot=True)
    event['edited_message'] = event.pop('message')
    core.collect()
    assert rows(core, 'SELECT conversation_id FROM inbox WHERE event_key=?', (f'tg:{event["update_id"]}',))[0][0] == conv


def test_many_fresh_roots_share_rolling_budget_across_restart_and_toggle(setup_pair):
    core, transport, clock, client = setup_pair
    core.bot_exchange_policy(client, 202, enabled=True)
    first_reserved = clock()
    for n in range(core.BOT_PAIR_SEND_LIMIT):
        conv, _ = receive(core, transport, client)
        core.send(client, 'one per root', f'root-{n}', conversation_id=conv)
        clock.advance(2)
    conv, _ = receive(core, transport, client)
    core = Core(core.state_dir, transport, clock=clock)
    core.bot_exchange_policy(client, 202, enabled=False)
    with pytest.raises(CoreError, match='bot_pair_limit'):
        core.send(client, 'disable is not a reset', 'disabled-blocked', conversation_id=conv)
    policy = core.bot_exchange_policy(client, 202, enabled=True)
    assert policy['reserved_in_window'] == 32
    assert policy['retry_at'] == first_reserved + core.BOT_PAIR_WINDOW
    before = rows(core, 'SELECT * FROM conversations')
    with pytest.raises(CoreError, match='bot_pair_limit'):
        core.send(client, 'too many roots', 'blocked-root', conversation_id=conv)
    assert not rows(core, 'SELECT id FROM outbox WHERE idempotency_key=?', ('blocked-root',))
    assert rows(core, 'SELECT * FROM conversations') == before
    clock.now = policy['retry_at']
    assert core.send(client, 'window has room', 'window-room', conversation_id=conv)['status'] == 'sent'
    assert core.bot_exchange_policy(client, 202)['reserved_in_window'] == 32


@pytest.mark.parametrize('outcome', ['unknown', 'reject'])
def test_uncertain_and_rejected_sends_reserve_budget_and_duplicates_do_not(setup_pair, outcome):
    core, transport, clock, client = setup_pair
    core.bot_exchange_policy(client, 202, enabled=True)
    conv, event = receive(core, transport, client)
    transport.outcomes.append(outcome)
    result = core.send(client, 'reply', 'same', conversation_id=conv)
    assert result['status'] in ('unknown', 'rejected')
    assert core.send(client, 'reply', 'same', conversation_id=conv) == result
    transport.updates.append(event)
    core.collect()
    assert core.bot_exchange_policy(client, 202)['reserved_in_window'] == 1
    assert len(rows(core, 'SELECT id FROM outbox')) == 1
    assert len(rows(core, 'SELECT id FROM inbox WHERE conversation_id=?', (conv,))) == 1


def test_pair_limit_is_transactional_under_concurrent_sends(setup_pair):
    core, transport, clock, client = setup_pair
    core.bot_exchange_policy(client, 202, enabled=True)
    for n in range(31):
        conv, _ = receive(core, transport, client)
        core.send(client, 'reply', f'prior-{n}', conversation_id=conv)
        clock.advance(2)
    candidates = [receive(core, transport, client)[0] for _ in range(2)]
    def send(item):
        n, conv = item
        try:
            return core.send(client, 'last slot', f'last-{n}', conversation_id=conv)['status']
        except CoreError as error:
            return error.code
    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(executor.map(send, enumerate(candidates)))
    assert results.count('bot_pair_limit') == 1
    assert core.bot_exchange_policy(client, 202)['reserved_in_window'] == 32


def test_policy_requires_scoped_bot_grant_and_leaves_other_pairs_independent(setup_pair):
    core, transport, clock, client = setup_pair
    for sender in (101, 303, transport.bot_id):
        with pytest.raises(CoreError, match='bot_sender_not_authorized'):
            core.bot_exchange_policy(client, sender, enabled=True)
    core.bot_exchange_policy(client, 202, enabled=True)
    other, _ = bind(core, transport, 'Other', -200)
    core.allow_sender(other, 202, is_bot=True)
    core.bot_exchange_policy(other, 202, enabled=True)
    conv, _ = receive(core, transport, client)
    core.send(client, 'reply', 'scoped', conversation_id=conv)
    assert core.bot_exchange_policy(other, 202)['reserved_in_window'] == 0
    # A bot display name in a different destination or with another ID grants nothing.
    count = len(rows(core, 'SELECT id FROM inbox'))
    append(transport, 'forged', sender=303, bot=True)
    append(transport, 'wrong group', sender=202, bot=True, chat=-999)
    core.collect()
    assert len(rows(core, 'SELECT id FROM inbox')) == count


def test_human_started_conversation_joins_pair_budget_on_real_bot_reply(setup_pair):
    core, transport, clock, client = setup_pair
    core.bot_exchange_policy(client, 202, enabled=True)
    human = append(transport, 'human request')
    core.collect()
    conv = rows(core, 'SELECT conversation_id FROM inbox WHERE event_key=?', (f'tg:{human["update_id"]}',))[0][0]
    sent = core.send(client, 'human reply', 'human-reply', conversation_id=conv)
    clock.advance(2)
    reply, _ = receive(core, transport, client, reply_to_message={'message_id': sent['message_id']})
    assert reply == conv
    assert core.bot_exchange_policy(client, 202)['reserved_in_window'] == 1


def test_activation_backfills_historical_human_started_bot_participation(setup_pair):
    core, transport, clock, client = setup_pair
    human = append(transport, 'earlier human request')
    core.collect()
    conv = rows(core, 'SELECT conversation_id FROM inbox WHERE event_key=?', (f'tg:{human["update_id"]}',))[0][0]
    sent = core.send(client, 'earlier reply', 'earlier-reply', conversation_id=conv)
    clock.advance(2)
    reply, _ = receive(core, transport, client, reply_to_message={'message_id': sent['message_id']})
    assert reply == conv
    core.send(client, 'earlier bot coordination', 'earlier-bot', conversation_id=conv)
    before = rows(core, 'SELECT * FROM conversations')
    assert core.bot_exchange_policy(client, 202, enabled=True)['reserved_in_window'] == 2
    assert rows(core, 'SELECT * FROM conversations') == before


def test_cli_policy_is_operator_only_and_requires_explicit_mode():
    args = parser().parse_args(['bot-exchange-policy', '--client-file', '/private/fake.json', '--sender-id', '202'])
    assert args.exchange_enabled is None
    assert parser().parse_args(['bot-exchange-policy', '--client-file', '/private/fake.json', '--sender-id', '202', '--enable']).exchange_enabled is True
    with pytest.raises(SystemExit):
        parser().parse_args(['bot-exchange-policy', '--client-file', '/private/fake.json', '--sender-id', '202', '--enable', '--disable'])


def test_never_enabled_disable_does_not_create_pair_budget(setup_pair):
    core, transport, clock, client = setup_pair
    for n in range(core.BOT_PAIR_SEND_LIMIT):
        human = append(transport, f'human {n}')
        core.collect()
        conv = rows(core, 'SELECT conversation_id FROM inbox WHERE event_key=?', (f'tg:{human["update_id"]}',))[0][0]
        sent = core.send(client, 'reply', f'human-reply-{n}', conversation_id=conv)
        clock.advance(2)
        reply, _ = receive(core, transport, client, reply_to_message={'message_id': sent['message_id']})
        assert reply == conv
        clock.advance(2)
    policy = core.bot_exchange_policy(client, 202, enabled=False)
    assert not policy['enabled']
    assert policy['reserved_in_window'] == 0
    assert not rows(core, 'SELECT 1 FROM bot_exchange_members WHERE client_id=? AND sender_id=?', (client, 202))
    human = append(transport, 'still unrestricted')
    core.collect()
    conv = rows(core, 'SELECT conversation_id FROM inbox WHERE event_key=?', (f'tg:{human["update_id"]}',))[0][0]
    assert core.send(client, 'not opted in', 'not-opted-in', conversation_id=conv)['status'] == 'sent'


def test_disable_retains_memberships_created_by_enablement(setup_pair):
    core, transport, clock, client = setup_pair
    core.bot_exchange_policy(client, 202, enabled=True)
    for n in range(core.BOT_PAIR_SEND_LIMIT):
        conv, _ = receive(core, transport, client)
        core.send(client, 'one per root', f'retained-{n}', conversation_id=conv)
        clock.advance(2)
    later, _ = receive(core, transport, client)
    policy = core.bot_exchange_policy(client, 202, enabled=False)
    assert not policy['enabled']
    assert policy['reserved_in_window'] == core.BOT_PAIR_SEND_LIMIT
    with pytest.raises(CoreError, match='bot_pair_limit'):
        core.send(client, 'disable is not a reset', 'retained-blocked', conversation_id=later)


def test_human_edit_keeps_captured_conversation_after_unknown_recovery(setup_pair):
    core, transport, clock, client = setup_pair
    transport.outcomes = ['unknown']
    sent = core.send(client, 'uncertain', 'uncertain')
    assert sent['status'] == 'unknown'
    human = append(transport, 'reply before recovery', reply_to_message={'message_id': 1})
    core.collect()
    original = rows(core, 'SELECT conversation_id FROM inbox WHERE event_key=?', (f'tg:{human["update_id"]}',))[0][0]
    assert original != sent['conversation_id']
    core.resolve_unknown(sent['outbox_id'], 'accepted', message_id=1)
    event = {'update_id': human['update_id'] + 1, 'edited_message': dict(human['message'], text='edited reply')}
    transport.updates.append(event)
    core.collect()
    edited = rows(core, 'SELECT conversation_id FROM inbox WHERE event_key=?', (f'tg:{event["update_id"]}',))[0][0]
    assert edited == original
    # The supplied conversation now matches the captured reply lineage, so this
    # is no longer reply_conversation_mismatch. Pacing may still queue it.
    result = core.send(client, 'follow the edit', 'follow-edit', conversation_id=edited,
                       reply_to=human['message']['message_id'])
    assert result['status'] in ('sent', 'queued')
    assert result['conversation_id'] == original


def test_reply_cannot_borrow_a_fresh_conversations_budget(setup_pair):
    core, transport, clock, client = setup_pair
    core.bot_exchange_policy(client, 202, enabled=True)
    old, event = receive(core, transport, client)
    send_n(core, clock, client, old, 'spent')
    fresh, _ = receive(core, transport, client)
    with pytest.raises(CoreError, match='reply_conversation_mismatch'):
        core.send(client, 'borrowed', 'borrowed', reply_to=event['message']['message_id'], conversation_id=fresh)
    with pytest.raises(CoreError, match='conversation_limit'):
        core.send(client, 'implicit old', 'implicit-old', reply_to=event['message']['message_id'])
    assert not rows(core, 'SELECT id FROM outbox WHERE idempotency_key IN (?,?)', ('borrowed', 'implicit-old'))


def test_clock_rollback_does_not_release_pair_reservations(setup_pair):
    core, transport, clock, client = setup_pair
    core.bot_exchange_policy(client, 202, enabled=True)
    for n in range(32):
        conv, _ = receive(core, transport, client)
        core.send(client, 'reserved', f'rollback-{n}', conversation_id=conv)
        clock.advance(2)
    conv, _ = receive(core, transport, client)
    clock.advance(-7200)
    with pytest.raises(CoreError, match='bot_pair_limit'):
        core.send(client, 'clock cannot reset', 'clock-reset', conversation_id=conv)
