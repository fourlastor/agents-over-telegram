"""Offline checks for explicit, scoped operator allowances."""
from concurrent.futures import ThreadPoolExecutor

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
        assert core._conversation_send_limit(db, other, conv, clock(), limits) == core.CONVERSATION_SEND_LIMIT
        assert core._conversation_send_limit(db, client, 'other', clock(), limits) == core.CONVERSATION_SEND_LIMIT


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


def test_cli_grant_keeps_interrupted_send(tmp_path):
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


def test_cli_exchange_limits_round_trip(tmp_path):
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
