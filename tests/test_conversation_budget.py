"""Offline checks for explicit, scoped operator allowances."""
from concurrent.futures import ThreadPoolExecutor
import json

import pytest

from agents_over_telegram.cli import parser, run
from agents_over_telegram.core import Core, CoreError
from agents_over_telegram.credentials import write_private_json
from test_bot_exchanges import setup_pair, receive, send_n
from test_core import append, bind, rows


def grant(core, client, conversation, sends=2, seconds=60):
    return core.conversation_budget(client, conversation, additional_sends=sends,
                                    duration_seconds=seconds, reason='Explicit operator request')


def test_grant_preserves_lineage_counters_and_idempotency_across_restart(setup_pair):
    core, transport, clock, client = setup_pair
    conv, _ = receive(core, transport, client)
    send_n(core, clock, client, conv, 'baseline')
    before = rows(core, 'SELECT * FROM conversations')
    with pytest.raises(CoreError, match='conversation_limit'):
        core.send(client, 'handoff', 'handoff', conversation_id=conv)
    assert grant(core, client, conv, sends=1)['remaining_sends'] == 1
    assert rows(core, 'SELECT * FROM conversations') == before
    core = Core(core.state_dir, transport, clock=clock)
    sent = core.send(client, 'handoff', 'handoff', conversation_id=conv)
    assert sent['status'] == 'sent' and sent['conversation_id'] == conv
    assert core.send(client, 'handoff', 'handoff', conversation_id=conv) == sent
    assert rows(core, 'SELECT send_count FROM conversations WHERE id=?', (conv,)) == [(core.CONVERSATION_SEND_LIMIT + 1,)]
    with pytest.raises(CoreError, match='conversation_limit'):
        core.send(client, 'extra', 'extra', conversation_id=conv)


def test_grant_expiry_and_clock_before_creation(setup_pair):
    core, transport, clock, client = setup_pair
    conv, _ = receive(core, transport, client)
    send_n(core, clock, client, conv, 'baseline')
    created = clock()
    grant(core, client, conv)
    clock.now = created + 60
    with pytest.raises(CoreError, match='conversation_limit'):
        core.send(client, 'late', 'late', conversation_id=conv)
    clock.now = created - 1
    with pytest.raises(CoreError, match='conversation_limit'):
        core.send(client, 'rollback', 'rollback', conversation_id=conv)


def test_allowance_is_scoped_to_client_and_existing_conversation(setup_pair):
    core, transport, clock, client = setup_pair
    conv, _ = receive(core, transport, client)
    send_n(core, clock, client, conv, 'baseline')
    grant(core, client, conv)
    other, _ = bind(core, transport, 'Other', chat=-200)
    with pytest.raises(CoreError, match='unknown_conversation'):
        grant(core, other, conv)
    with pytest.raises(CoreError, match='unknown_conversation'):
        grant(core, client, 'invented')
    with core._db() as db:
        limits = core._limits(db)
        assert core._conversation_send_limit(db, 'other', clock(), limits) == core.CONVERSATION_SEND_LIMIT


def test_concurrent_last_slot_is_reserved_once(setup_pair):
    core, transport, clock, client = setup_pair
    conv, _ = receive(core, transport, client)
    send_n(core, clock, client, conv, 'baseline')
    grant(core, client, conv, sends=1)

    def send(index):
        try:
            return core.send(client, 'handoff', f'concurrent-{index}', conversation_id=conv)['status']
        except CoreError as error:
            return str(error)

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(send, range(2)))
    assert results.count('conversation_limit') == 1
    assert len(rows(core, "SELECT id FROM outbox WHERE idempotency_key LIKE 'concurrent-%'")) == 1


def test_pair_limit_and_pause_still_apply(setup_pair):
    core, transport, clock, client = setup_pair
    core.bot_exchange_policy(client, 202, enabled=True)
    conv, _ = receive(core, transport, client)
    grant(core, client, conv, sends=32, seconds=3600)
    send_n(core, clock, client, conv, 'reserved', count=32)
    grant(core, client, conv, sends=1)
    with pytest.raises(CoreError, match='bot_pair_limit'):
        core.send(client, 'too many', 'too-many', conversation_id=conv)
    core.pause(client)
    with pytest.raises(CoreError, match='connection_paused'):
        grant(core, client, conv)


@pytest.mark.parametrize('sends,seconds', [(0, 60), (33, 60), (True, 60), (1, 0), (1, 86401)])
def test_grants_reject_unbounded_or_invalid_values(setup_pair, sends, seconds):
    core, transport, clock, client = setup_pair
    conv, _ = receive(core, transport, client)
    with pytest.raises(CoreError, match='invalid_conversation_budget'):
        grant(core, client, conv, sends, seconds)
    assert not rows(core, 'SELECT id FROM conversation_budget_grants')


@pytest.fixture
def operator_says_yes(monkeypatch):
    import agents_over_telegram.cli as cli
    monkeypatch.setattr(cli, '_confirm_operator', lambda summary: None)


def test_cli_grant_keeps_interrupted_send(tmp_path, operator_says_yes):
    from agents_over_telegram.transport import FakeTransport
    core = Core(tmp_path / 'state', FakeTransport())
    core.register_account(900001, 'aot_test_bot')
    client, credentials = bind(core, core.transport)
    client_file = tmp_path / 'client.json'
    write_private_json(client_file, credentials)
    append(core.transport, 'Existing work')
    core.collect()
    conv = rows(core, 'SELECT conversation_id FROM inbox WHERE client_id=?', (client,))[0][0]
    with core._tx() as db:
        db.execute("INSERT INTO outbox(id,client_id,chat_id,thread_id,idempotency_key,request_hash,text,conversation_id,status,created,updated,due) VALUES('interrupted',?,-100,0,'interrupted','hash','text',?,'sending',1,1,1)", (client, conv))
    args = ['--state-dir', str(core.state_dir), '--fake', 'conversation-budget',
            '--client-file', str(client_file), '--conversation-id', conv]
    assert run(parser().parse_args(args))['send_limit'] == core.CONVERSATION_SEND_LIMIT
    result = run(parser().parse_args(args + ['--additional-sends', '16', '--duration-seconds', '50400', '--reason', 'User requested Toni handoff']))
    assert result['send_limit'] == core.CONVERSATION_SEND_LIMIT + 16
    assert result['remaining_sends'] == core.CONVERSATION_SEND_LIMIT + 16
    assert rows(core, "SELECT status FROM outbox WHERE id='interrupted'") == [('sending',)]
    assert len(result['grants']) == 1


def test_operator_limits_apply_to_every_process_and_reset_to_defaults(setup_pair):
    core, transport, clock, client = setup_pair
    conv, _ = receive(core, transport, client)
    assert core.exchange_limits()['source'] == 'default'
    core.exchange_limits(conversation_send_limit=3, conversation_window_seconds=600, reason='Tight test limit')
    other_process = Core(core.state_dir, transport, clock=clock)
    first = clock()
    send_n(other_process, clock, client, conv, 'tight', count=3)
    with pytest.raises(CoreError, match='conversation_limit') as blocked:
        core.send(client, 'over', 'over', conversation_id=conv)
    assert blocked.value.details == {'sent_in_window': 3, 'limit': 3, 'window_seconds': 600,
                                     'retry_at': first + 600, 'retry_after_seconds': 600 - 6}
    assert grant(core, client, conv, sends=2)['send_limit'] == 5
    assert core.send(client, 'granted', 'granted', conversation_id=conv)['status'] == 'sent'
    clock.advance(2)
    core.exchange_limits(reset=True, reason='Back to defaults')
    assert core.exchange_limits()['conversation_send_limit'] == core.CONVERSATION_SEND_LIMIT
    assert core.send(client, 'default again', 'default', conversation_id=conv)['status'] == 'sent'


def test_pair_limit_is_configurable_and_reports_details(setup_pair):
    core, transport, clock, client = setup_pair
    core.bot_exchange_policy(client, 202, enabled=True)
    core.exchange_limits(pair_send_limit=2, pair_window_seconds=120, reason='Tight pair')
    for n in range(2):
        conv, _ = receive(core, transport, client)
        core.send(client, 'one', f'pair-{n}', conversation_id=conv)
        clock.advance(2)
    conv, _ = receive(core, transport, client)
    with pytest.raises(CoreError, match='bot_pair_limit') as blocked:
        core.send(client, 'over', 'pair-over', conversation_id=conv)
    assert blocked.value.details['limit'] == 2 and blocked.value.details['window_seconds'] == 120
    policy = core.bot_exchange_policy(client, 202)
    assert (policy['pair_send_limit'], policy['window_seconds']) == (2, 120)
    clock.now = blocked.value.retry_at
    assert core.send(client, 'later', 'pair-later', conversation_id=conv)['status'] == 'sent'


@pytest.mark.parametrize('kwargs', [
    {'conversation_send_limit': 0, 'reason': 'x'}, {'conversation_send_limit': 1001, 'reason': 'x'},
    {'conversation_window_seconds': 59, 'reason': 'x'}, {'pair_window_seconds': 86401, 'reason': 'x'},
    {'pair_send_limit': True, 'reason': 'x'}, {'conversation_send_limit': 5},
    {'conversation_send_limit': 5, 'reason': '   '}, {'reason': 'nothing to change'},
    {'reset': True, 'conversation_send_limit': 5, 'reason': 'x'},
])
def test_exchange_limits_reject_invalid_changes(setup_pair, kwargs):
    core = setup_pair[0]
    with pytest.raises(CoreError, match='invalid_exchange_limits'):
        core.exchange_limits(**kwargs)
    assert core.exchange_limits()['source'] == 'default'


def test_cli_exchange_limits_round_trip(tmp_path, operator_says_yes):
    from agents_over_telegram.transport import FakeTransport
    core = Core(tmp_path / 'state', FakeTransport())
    core.register_account(900001, 'aot_test_bot')
    base = ['--state-dir', str(core.state_dir), '--fake', 'exchange-limits']
    assert run(parser().parse_args(base))['conversation_send_limit'] == core.CONVERSATION_SEND_LIMIT
    changed = run(parser().parse_args(base + ['--conversation-send-limit', '60', '--reason', 'Long agent runs']))
    assert changed['conversation_send_limit'] == 60 and changed['source'] == 'operator'
    assert run(parser().parse_args(base + ['--reset', '--reason', 'Undo']))['source'] == 'default'


def test_legacy_lifetime_grants_do_not_change_rolling_limits(setup_pair):
    core, transport, clock, client = setup_pair
    conv, _ = receive(core, transport, client)
    with core._tx() as db:
        db.execute('INSERT INTO conversations(id) VALUES(?) ON CONFLICT DO NOTHING', (conv,))
        db.execute('INSERT INTO conversation_budget_grants(client_id,conversation_id,send_limit,created,expires_at,reason)'
                   ' VALUES(?,?,?,?,?,?)', (client, conv, 500, clock(), clock() + 600, 'old lifetime grant'))
    assert core.conversation_budget(client, conv)['send_limit'] == core.CONVERSATION_SEND_LIMIT


def human_conversations(core, transport, client, count):
    conversations = []
    for n in range(count):
        append(transport, f'owner request {n}')
        core.collect()
        conversations.append(rows(core, 'SELECT conversation_id FROM inbox WHERE client_id=? ORDER BY created DESC,rowid DESC LIMIT 1',
                                  (client,))[0][0])
    return conversations


def send_all(core, clock, client, conversations, rounds, prefix):
    """Try to send `rounds` times into every conversation; return the successes."""
    sent = 0
    for r in range(rounds):
        for n, conv in enumerate(conversations):
            try:
                core.send(client, 'loop', f'{prefix}-{r}-{n}', conversation_id=conv)
                sent += 1
            except CoreError as error:
                assert error.code == 'client_limit', error.code
            clock.advance(1)
    return sent


def test_rotating_old_conversations_cannot_exceed_the_agent_ceiling(setup_pair):
    # Review reproduction: ten old conversations used to allow 300 sends an hour.
    core, transport, clock, client = setup_pair
    conversations = human_conversations(core, transport, client, 10)
    start = clock()
    assert send_all(core, clock, client, conversations, 30, 'hour1') == core.CLIENT_SEND_LIMIT
    assert clock() - start < core.CLIENT_WINDOW
    with pytest.raises(CoreError, match='client_limit') as blocked:
        core.send(client, 'over', 'over', conversation_id=conversations[0])
    details = blocked.value.details
    assert (details['limit'], details['window_seconds'], details['sent_in_window']) == (60, 3600, 60)
    # The ceiling refills with time and does not grow with the number of conversations.
    clock.now = start + 3 * core.CLIENT_WINDOW
    assert send_all(core, clock, client, conversations, 30, 'later') == core.CLIENT_SEND_LIMIT


def test_bot_replying_to_old_messages_is_bounded_with_default_settings(setup_pair):
    # Review reproduction: an allowed bot replying to ten old agent messages,
    # without the bot exchange policy, used to drive 300 agent sends an hour.
    core, transport, clock, client = setup_pair
    conversations = human_conversations(core, transport, client, 10)
    sent = []
    for n, conv in enumerate(conversations):
        sent.append(core.send(client, 'first', f'first-{n}', conversation_id=conv))
        assert sent[-1]['status'] == 'sent'
        clock.advance(2)
    allowed = len(sent)
    for r in range(30):
        for n, first in enumerate(sent):
            conv, _ = receive(core, transport, client, reply_to_message={'message_id': first['message_id']})
            assert conv == conversations[n]
            try:
                core.send(client, 'reply', f'reply-{r}-{n}', conversation_id=conv)
                allowed += 1
            except CoreError as error:
                assert error.code == 'client_limit'
        clock.advance(10)
    assert allowed == core.CLIENT_SEND_LIMIT


def test_client_ceiling_is_configurable_and_counts_broker_sends(setup_pair):
    core, transport, clock, client = setup_pair
    core.exchange_limits(client_send_limit=3, client_window_seconds=300, reason='Tight agent ceiling')
    conversations = human_conversations(core, transport, client, 3)
    assert send_all(core, clock, client, conversations, 2, 'tight') == 3
    assert core.exchange_limits()['client_send_limit'] == 3


def test_grant_raises_the_limit_for_every_sender_in_the_conversation(setup_pair):
    core, transport, clock, client = setup_pair
    peer, _ = bind(core, transport, 'Peer', chat=-200)
    core.grant_peer(client, peer)
    core.grant_peer(peer, client)
    core.exchange_limits(conversation_send_limit=2, reason='Small window')
    conv = core.send(client, 'hello peer', 'hello', recipient=peer)['conversation_id']
    clock.advance(2)
    core.send(peer, 'hello back', 'back', recipient=client, conversation_id=conv)
    clock.advance(2)
    with pytest.raises(CoreError, match='conversation_limit'):
        core.send(client, 'third', 'third', recipient=peer, conversation_id=conv)
    grant(core, client, conv, sends=2)
    core.send(client, 'third', 'third', recipient=peer, conversation_id=conv)
    clock.advance(2)
    # The peer was not the granting client, but its sends share the window and the grant.
    assert core.send(peer, 'fourth', 'fourth', recipient=client, conversation_id=conv)['conversation_id'] == conv


def test_bot_pair_limit_reports_the_latest_retry_time(setup_pair):
    core, transport, clock, client = setup_pair
    core.allow_sender(client, 303, is_bot=True)
    core.bot_exchange_policy(client, 202, enabled=True)
    core.bot_exchange_policy(client, 303, enabled=True)
    core.exchange_limits(pair_send_limit=3, pair_window_seconds=120, reason='Small pair budget')
    start = clock()
    only_202, _ = receive(core, transport, client)
    shared, _ = receive(core, transport, client)
    core.send(client, 'a', 'a0', conversation_id=only_202)
    clock.advance(2)
    core.send(client, 'a', 'a1', conversation_id=only_202)
    clock.advance(2)
    joined = core.send(client, 's', 's0', conversation_id=shared)
    clock.advance(2)
    reply, _ = receive(core, transport, client, sender=303, reply_to_message={'message_id': joined['message_id']})
    assert reply == shared
    only_303, _ = receive(core, transport, client, sender=303)
    core.send(client, 'b', 'b0', conversation_id=only_303)
    clock.advance(2)
    core.send(client, 'b', 'b1', conversation_id=only_303)
    clock.advance(2)
    with pytest.raises(CoreError, match='bot_pair_limit') as blocked:
        core.send(client, 's', 's1', conversation_id=shared)
    # Pair 202 frees a slot at start+120; pair 303 only at start+4+120.
    assert blocked.value.retry_at == start + 4 + 120


@pytest.mark.parametrize('tty,answer,expected', [(False, None, 'operator_confirmation_required'),
                                                 (True, 'no', 'operator_confirmation_declined'),
                                                 (True, 'yes', None)])
def test_cli_limit_changes_need_a_human_at_a_terminal(tmp_path, monkeypatch, tty, answer, expected):
    from agents_over_telegram.credentials import CredentialError
    from agents_over_telegram.transport import FakeTransport
    core = Core(tmp_path / 'state', FakeTransport())
    core.register_account(900001, 'aot_test_bot')
    monkeypatch.setattr('sys.stdin.isatty', lambda: tty)
    monkeypatch.setattr('builtins.input', lambda: answer)
    base = ['--state-dir', str(core.state_dir), '--fake', 'exchange-limits']
    # Inspecting never asks.
    assert run(parser().parse_args(base))['source'] == 'default'
    change = base + ['--client-send-limit', '500', '--reason', 'Agent asked for more']
    if expected:
        with pytest.raises(CredentialError, match=expected):
            run(parser().parse_args(change))
        assert core.exchange_limits()['source'] == 'default'
    else:
        assert run(parser().parse_args(change))['client_send_limit'] == 500


def owner_conversation_at_limit(core, transport, clock, client):
    append(transport, 'owner starts work')
    core.collect()
    conv = rows(core, 'SELECT conversation_id FROM inbox WHERE client_id=? ORDER BY created DESC,rowid DESC LIMIT 1',
                (client,))[0][0]
    sent = send_n(core, clock, client, conv, 'fill')
    with pytest.raises(CoreError, match='conversation_limit'):
        core.send(client, 'blocked', 'blocked', conversation_id=conv)
    return conv, sent


def test_owner_reply_restores_the_conversation_window(setup_pair):
    # Seen live: the owner replied to an agent message in a blocked conversation,
    # and the next send in that conversation was still refused.
    core, transport, clock, client = setup_pair
    conv, sent = owner_conversation_at_limit(core, transport, clock, client)
    append(transport, '@agent carry on', reply_to_message={'message_id': sent[-1]['message_id']})
    core.collect()
    assert rows(core, 'SELECT conversation_id FROM inbox ORDER BY created DESC,rowid DESC LIMIT 1') == [(conv,)]
    assert core.conversation_budget(client, conv)['remaining_sends'] == core.CONVERSATION_SEND_LIMIT
    clock.advance(2)
    assert core.send(client, 'thanks', 'after-owner', conversation_id=conv)['conversation_id'] == conv


@pytest.mark.parametrize('who', ['participant', 'bot', 'owner_edit'])
def test_only_a_new_owner_message_restores_the_window(setup_pair, who):
    core, transport, clock, client = setup_pair
    core.allow_sender(client, 303, is_bot=False)
    conv, sent = owner_conversation_at_limit(core, transport, clock, client)
    reply = {'reply_to_message': {'message_id': sent[-1]['message_id']}}
    if who == 'participant':
        append(transport, 'not the owner', sender=303, **reply)
    elif who == 'bot':
        append(transport, 'a bot', sender=202, bot=True, **reply)
    else:
        original = rows(core, "SELECT payload FROM inbox WHERE client_id=? AND json_extract(payload,'$.text')='owner starts work'",
                        (client,))[0][0]
        number = json.loads(original)['message_id']
        event = append(transport, 'edited')
        event['edited_message'] = dict(event.pop('message'), message_id=number, edit_date=int(clock()))
    core.collect()
    clock.advance(2)
    with pytest.raises(CoreError, match='conversation_limit'):
        core.send(client, 'still blocked', 'still', conversation_id=conv)


def test_owner_reply_does_not_reset_the_agent_ceiling(setup_pair):
    core, transport, clock, client = setup_pair
    core.exchange_limits(conversation_send_limit=2, client_send_limit=3, reason='Small limits')
    append(transport, 'owner starts work')
    core.collect()
    conv = rows(core, 'SELECT conversation_id FROM inbox ORDER BY created DESC,rowid DESC LIMIT 1')[0][0]
    sent = send_n(core, clock, client, conv, 'a', count=2)
    append(transport, 'go on', reply_to_message={'message_id': sent[-1]['message_id']})
    core.collect()
    clock.advance(2)
    core.send(client, 'third', 'third', conversation_id=conv)
    clock.advance(2)
    with pytest.raises(CoreError, match='client_limit'):
        core.send(client, 'fourth', 'fourth', conversation_id=conv)


def test_owner_reset_uses_message_time_and_ignores_reprocessed_updates(setup_pair):
    core, transport, clock, client = setup_pair
    conv, sent = owner_conversation_at_limit(core, transport, clock, client)
    written = int(clock()) - 5
    event = append(transport, 'go on', date=written, reply_to_message={'message_id': sent[-1]['message_id']})
    core.collect()
    assert rows(core, 'SELECT window_reset_at FROM conversations WHERE id=?', (conv,)) == [(written,)]
    clock.advance(2)
    after = core.send(client, 'after reset', 'after-reset', conversation_id=conv)
    clock.advance(30)
    later = int(clock())
    append(transport, 'and again', date=later, reply_to_message={'message_id': after['message_id']})
    core.collect()
    assert rows(core, 'SELECT window_reset_at FROM conversations WHERE id=?', (conv,)) == [(later,)]
    # Collecting the first update again must not move the reset back to its time.
    transport.updates[:] = [event]
    with core._tx() as db:
        db.execute('UPDATE account SET cursor=?', (event['update_id'],))
    core.collect()
    assert rows(core, 'SELECT window_reset_at FROM conversations WHERE id=?', (conv,)) == [(later,)]
