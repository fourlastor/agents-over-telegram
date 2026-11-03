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
    assert rows(core, 'SELECT send_count FROM conversations WHERE id=?', (conv,)) == [(9,)]
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
        assert core._conversation_send_limit(db, other, conv, clock()) == 8
        assert core._conversation_send_limit(db, client, 'other', clock()) == 8


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
    assert run(parser().parse_args(args))['send_limit'] == 8
    result = run(parser().parse_args(args + ['--additional-sends', '16', '--duration-seconds', '50400', '--reason', 'User requested Toni handoff']))
    assert result['remaining_sends'] == 16
    assert rows(core, "SELECT status FROM outbox WHERE id='interrupted'") == [('sending',)]
    assert len(result['grants']) == 1
