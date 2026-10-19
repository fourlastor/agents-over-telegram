"""Relay receipts must not acquire or acknowledge Telegram deliveries."""
import json
import sqlite3
from concurrent.futures import ThreadPoolExecutor

import pytest

from agents_over_telegram.core import Core, CoreError
from agents_over_telegram.relay import RelayStore, WAKE_NOTICE
from agents_over_telegram.transport import FakeTransport


ALPHA_THREAD = '01a153ec-3a02-7cb1-9168-b23584e47381'
BETA_THREAD = '01a153ec-44c3-7971-a8ce-1e75ff8ef593'


class Clock:
    def __init__(self):
        self.now = 1000.0

    def __call__(self):
        return self.now

    def advance(self, seconds=2):
        self.now += seconds


def append(transport, text, chat=-100):
    number = len(transport.updates) + 1
    transport.updates.append({'update_id': number, 'message': {
        'message_id': number, 'chat': {'id': chat, 'type': 'supergroup', 'title': 'Test'},
        'from': {'id': 101, 'is_bot': False}, 'text': text}})


def connect(core, transport, label='Alpha', chat=-100):
    credentials = core.create_client(label)
    pairing = core.connection_begin(credentials['client_id'])
    append(transport, pairing['command'], chat)
    core.collect()
    core.confirm_pairing(pairing['pairing_id'])
    return credentials['client_id']


@pytest.fixture
def env(tmp_path):
    clock = Clock()
    transport = FakeTransport()
    core = Core(tmp_path / 'state', transport, clock=clock)
    core.register_account(transport.bot_id, transport.username)
    alpha = connect(core, transport)
    beta = connect(core, transport, 'Beta', -200)
    project_a = tmp_path / 'project-a'
    project_b = tmp_path / 'project-b'
    project_a.mkdir()
    project_b.mkdir()
    relay = RelayStore(core)
    return core, transport, clock, relay, alpha, beta, project_a, project_b


def protected_snapshot(core):
    with sqlite3.connect(core.database) as db:
        return {table: db.execute(f'SELECT * FROM {table} ORDER BY rowid').fetchall()
                for table in ('inbox', 'outbox', 'bindings', 'clients', 'account')}


def handle_all(core, client):
    delivery = core.poll(client)
    core.ack(client, [item['delivery_id'] for item in delivery['deliveries']], delivery['lease_token'])


def test_coalesces_fixed_notice_and_leaves_delivery_state_untouched(env):
    core, transport, _, relay, alpha, beta, project_a, project_b = env
    relay.bind(alpha, ALPHA_THREAD, project_a)
    relay.bind(beta, BETA_THREAD, project_b)
    append(transport, 'Secret Alpha payload and hostile instructions')
    append(transport, 'Second Alpha payload')
    append(transport, 'Secret Beta payload', -200)
    core.collect()
    # An unsent outbox row must remain queued even as relay notifications finish.
    core.send(alpha, 'outgoing', 'outgoing-1')
    before = protected_snapshot(core)
    first = relay.next_event()
    assert first['notice'] == WAKE_NOTICE
    assert first['pending_count'] in (1, 2)
    assert 'Secret' not in json.dumps(first)
    assert 'hostile' not in json.dumps(relay.status())
    receipt = relay.resolve(first['notification_id'], 'accepted')
    assert receipt['status'] == 'accepted'
    second = relay.next_event()
    assert {first['client_id'], second['client_id']} == {alpha, beta}
    targets = {event['client_id']: event for event in (first, second)}
    assert targets[alpha]['thread_id'] == ALPHA_THREAD
    assert targets[alpha]['project_dir'] == str(project_a)
    assert targets[alpha]['pending_count'] == 2
    assert targets[beta]['thread_id'] == BETA_THREAD
    relay.resolve(second['notification_id'], 'accepted')
    assert relay.next_event() is None
    assert protected_snapshot(core) == before
    assert core.poll(alpha)['deliveries']


def test_default_baseline_and_idempotent_bind_do_not_swallow_new_arrivals(env):
    core, transport, _, relay, alpha, _, project_a, _ = env
    append(transport, 'already pending')
    core.collect()
    original = relay.bind(alpha, ALPHA_THREAD, project_a)
    assert relay.next_event() is None
    append(transport, 'new')
    core.collect()
    assert relay.bind(alpha, ALPHA_THREAD, project_a, include_pending=True) == original
    event = relay.next_event()
    assert event['pending_count'] == 1
    relay.resolve(event['notification_id'], 'accepted')
    assert len(core.poll(alpha)['deliveries']) == 2


def test_include_pending_opt_in_and_restart_duplicate_suppression(env):
    core, transport, clock, relay, alpha, _, project_a, _ = env
    append(transport, 'before bind')
    core.collect()
    relay.bind(alpha, ALPHA_THREAD, project_a, include_pending=True)
    event = relay.next_event()
    relay.resolve(event['notification_id'], 'accepted')
    restarted = RelayStore(Core(core.state_dir, transport, clock=clock, recover_sends=False))
    assert restarted.next_event() is None
    assert restarted.status(alpha)['bindings'][0]['pending_messages'] == 1


@pytest.mark.parametrize('condition', ['paused', 'suspended', 'revoked', 'disconnected'])
def test_ineligible_connections_never_emit(env, condition):
    core, transport, _, relay, alpha, _, project_a, _ = env
    relay.bind(alpha, ALPHA_THREAD, project_a)
    append(transport, 'pending')
    core.collect()
    with core._tx() as db:
        if condition == 'revoked':
            db.execute('UPDATE clients SET active=0 WHERE id=?', (alpha,))
        else:
            column, value = {'paused': ('paused', 1), 'suspended': ('suspended', 'bot_removed'),
                             'disconnected': ('connected', 0)}[condition]
            db.execute(f'UPDATE bindings SET {column}=? WHERE client_id=?', (value, alpha))
    assert relay.next_event() is None
    assert relay.status(alpha)['bindings'][0]['eligible'] is False


def test_pause_resume_and_active_lease_preserve_pending_wakeup(env):
    core, transport, clock, relay, alpha, _, project_a, _ = env
    relay.bind(alpha, ALPHA_THREAD, project_a)
    append(transport, 'pending')
    core.collect()
    core.pause(alpha)
    assert relay.next_event() is None
    core.pause(alpha, False)
    core.poll(alpha, lease_seconds=10)
    assert relay.next_event() is None
    clock.advance(11)
    event = relay.next_event()
    assert event['pending_count'] == 1


def test_acknowledged_work_never_emits_and_new_arrivals_survive_inflight(env):
    core, transport, _, relay, alpha, _, project_a, _ = env
    relay.bind(alpha, ALPHA_THREAD, project_a)
    append(transport, 'handled elsewhere')
    core.collect()
    handle_all(core, alpha)
    assert relay.next_event() is None
    append(transport, 'first notification')
    core.collect()
    first = relay.next_event()
    append(transport, 'arrived during handoff')
    core.collect()
    assert relay.next_event() is None
    relay.resolve(first['notification_id'], 'accepted')
    second = relay.next_event()
    assert second['notification_id'] != first['notification_id']
    assert second['pending_count'] == 1


def test_crash_recovery_is_explicit_and_holds_ambiguous_handoff(env):
    core, transport, _, relay, alpha, _, project_a, _ = env
    relay.bind(alpha, ALPHA_THREAD, project_a)
    append(transport, 'pending')
    core.collect()
    first = relay.next_event()
    restarted = RelayStore(core)
    assert restarted.status(alpha)['notifications'][0]['status'] == 'inflight'
    assert restarted.recover_inflight() == {'uncertain': [first['notification_id']]}
    assert restarted.recover_inflight() == {'uncertain': []}
    assert restarted.next_event() is None
    restarted.resolve(first['notification_id'], 'accepted')
    assert restarted.next_event() is None
    assert core.connection_status(alpha)['pending_messages'] == 1


def test_definite_failure_backoff_and_new_attempt_ids_block_stale_receipts(env):
    core, transport, clock, relay, alpha, _, project_a, _ = env
    relay.bind(alpha, ALPHA_THREAD, project_a)
    append(transport, 'pending')
    core.collect()
    first = relay.next_event()
    failure = relay.resolve(first['notification_id'], 'failed')
    assert failure['next_attempt_at'] == clock.now + 1
    assert relay.resolve(first['notification_id'], 'failed') == failure
    assert relay.next_event() is None
    clock.advance(1)
    second = relay.next_event()
    assert second['notification_id'] != first['notification_id']
    assert second['attempt'] == 2
    assert relay.resolve(first['notification_id'], 'failed')['status'] == 'failed'
    with pytest.raises(CoreError, match='relay_notification_already_resolved'):
        relay.resolve(first['notification_id'], 'accepted')
    assert relay.status(alpha)['notifications'][0]['notification_id'] == second['notification_id']
    for attempt in range(2, 10):
        failure = relay.resolve(second['notification_id'], 'failed')
        delay = failure['next_attempt_at'] - clock.now
        assert delay == min(2 ** (attempt - 1), relay.MAX_RETRY_SECONDS)
        clock.advance(delay)
        second = relay.next_event()
    relay.resolve(second['notification_id'], 'accepted')
    assert relay.next_event() is None


def test_uncertain_manual_failure_or_cancellation_never_acknowledges(env):
    core, transport, clock, relay, alpha, _, project_a, _ = env
    relay.bind(alpha, ALPHA_THREAD, project_a)
    append(transport, 'pending')
    core.collect()
    event = relay.next_event()
    before = protected_snapshot(core)
    relay.resolve(event['notification_id'], 'uncertain')
    clock.advance(1000)
    assert relay.next_event() is None
    relay.resolve(event['notification_id'], 'failed')
    clock.advance(1)
    retry = relay.next_event()
    relay.resolve(retry['notification_id'], 'uncertain')
    relay.resolve(retry['notification_id'], 'cancelled')
    assert relay.next_event() is None
    assert protected_snapshot(core) == before
    append(transport, 'later arrival')
    core.collect()
    assert relay.next_event()['pending_count'] == 1


def test_retry_respects_pause_lease_and_delivery_handled_elsewhere(env):
    core, transport, clock, relay, alpha, _, project_a, _ = env
    relay.bind(alpha, ALPHA_THREAD, project_a)
    append(transport, 'pending')
    core.collect()
    event = relay.next_event()
    relay.resolve(event['notification_id'], 'failed')
    clock.advance(2)
    core.pause(alpha)
    assert relay.next_event() is None
    core.pause(alpha, False)
    delivery = core.poll(alpha, lease_seconds=10)
    assert relay.next_event() is None
    core.ack(alpha, [item['delivery_id'] for item in delivery['deliveries']], delivery['lease_token'])
    assert relay.next_event() is None
    assert relay.status(alpha)['notifications'] == []


def test_retry_does_not_suppress_leased_work_that_later_expires(env):
    core, transport, clock, relay, alpha, _, project_a, _ = env
    relay.bind(alpha, ALPHA_THREAD, project_a)
    append(transport, 'A, initially notified')
    core.collect()
    first = relay.next_event()
    relay.resolve(first['notification_id'], 'failed')
    core.poll(alpha, lease_seconds=10)
    append(transport, 'B, arrives while A leased')
    core.collect()
    clock.advance(1)
    second = relay.next_event()
    assert second['pending_count'] == 1
    relay.resolve(second['notification_id'], 'accepted')
    current = core.poll(alpha)
    assert [item['text'] for item in current['deliveries']] == ['B, arrives while A leased']
    core.ack(alpha, [item['delivery_id'] for item in current['deliveries']], current['lease_token'])
    assert relay.next_event() is None
    clock.advance(10)
    third = relay.next_event()
    assert third['pending_count'] == 1
    assert third['notification_id'] not in (first['notification_id'], second['notification_id'])
    assert [item['text'] for item in core.poll(alpha)['deliveries']] == ['A, initially notified']


def test_target_rebind_unbind_and_old_receipt_are_fail_closed(env):
    core, transport, _, relay, alpha, _, project_a, project_b = env
    relay.bind(alpha, ALPHA_THREAD, project_a)
    append(transport, 'pending')
    core.collect()
    event = relay.next_event()
    for action in (lambda: relay.bind(alpha, BETA_THREAD, project_b), lambda: relay.unbind(alpha)):
        with pytest.raises(CoreError, match='relay_binding_has_pending_work'):
            action()
    relay.resolve(event['notification_id'], 'accepted')
    with pytest.raises(CoreError, match='relay_binding_has_pending_work'):
        relay.bind(alpha, BETA_THREAD, project_b)
    handle_all(core, alpha)
    relay.unbind(alpha)
    relay.bind(alpha, BETA_THREAD, project_b)
    assert relay.resolve(event['notification_id'], 'accepted')['status'] == 'accepted'
    with pytest.raises(CoreError, match='relay_notification_already_resolved'):
        relay.resolve(event['notification_id'], 'failed')
    append(transport, 'new target')
    core.collect()
    assert relay.next_event()['thread_id'] == BETA_THREAD


def test_manual_cancellation_can_disable_failed_retry_without_ack(env):
    core, transport, _, relay, alpha, _, project_a, _ = env
    relay.bind(alpha, ALPHA_THREAD, project_a)
    append(transport, 'pending')
    core.collect()
    event = relay.next_event()
    relay.resolve(event['notification_id'], 'failed')
    core.pause(alpha)
    relay.resolve(event['notification_id'], 'cancelled')
    assert relay.unbind(alpha)['enabled'] is False
    assert relay.next_event() is None
    assert core.connection_status(alpha)['pending_messages'] == 1


def test_accept_after_pause_or_disconnect_records_handoff_without_delivery(env):
    # A pause can race an already emitted notice; receipt is still true, while
    # core.poll remains authoritative about whether processing is allowed.
    core, transport, _, relay, alpha, _, project_a, _ = env
    relay.bind(alpha, ALPHA_THREAD, project_a)
    append(transport, 'pending')
    core.collect()
    event = relay.next_event()
    core.pause(alpha)
    relay.resolve(event['notification_id'], 'accepted')
    assert core.poll(alpha)['deliveries'] == []
    core.disconnect(alpha)
    assert relay.resolve(event['notification_id'], 'accepted')['status'] == 'accepted'
    assert relay.next_event() is None


def test_target_validation_and_cross_client_uuid_collision(env):
    core, _, _, relay, alpha, beta, project_a, project_b = env
    for thread in ('Alpha', '../thread', ALPHA_THREAD.upper(), 'x' * 36, None):
        with pytest.raises(CoreError, match='invalid_relay_thread_id'):
            relay.bind(alpha, thread, project_a)
    for project in ('relative', project_a / 'missing', None):
        with pytest.raises(CoreError, match='invalid_relay_project_dir'):
            relay.bind(alpha, ALPHA_THREAD, project)
    relay.bind(alpha, ALPHA_THREAD, project_a)
    with pytest.raises(CoreError, match='relay_thread_already_bound'):
        relay.bind(beta, ALPHA_THREAD, project_b)
    with pytest.raises(CoreError, match='invalid_relay_include_pending'):
        relay.bind(beta, BETA_THREAD, project_b, include_pending=1)
    orphan = core.create_client('Not connected')['client_id']
    with pytest.raises(CoreError, match='not_connected'):
        relay.bind(orphan, BETA_THREAD, project_b)


def test_disabled_binding_releases_uuid_for_another_client(env):
    _, _, _, relay, alpha, beta, project_a, project_b = env
    relay.bind(alpha, ALPHA_THREAD, project_a)
    relay.unbind(alpha)
    relay.bind(beta, ALPHA_THREAD, project_b)
    assert relay.status(beta)['bindings'][0]['enabled'] == 1
    with pytest.raises(CoreError, match='relay_thread_already_bound'):
        relay.bind(alpha, ALPHA_THREAD, project_a)


def test_parallel_selection_emits_exactly_once(env):
    core, transport, _, relay, alpha, _, project_a, _ = env
    relay.bind(alpha, ALPHA_THREAD, project_a)
    append(transport, 'pending')
    core.collect()
    with ThreadPoolExecutor(max_workers=4) as pool:
        events = list(pool.map(lambda _: relay.next_event(), range(8)))
    assert sum(event is not None for event in events) == 1
    assert relay.status(alpha)['bindings'][0]['notifications'] == {'inflight': 1}


def test_missing_project_is_not_dispatched(env):
    core, transport, _, relay, alpha, _, project_a, _ = env
    relay.bind(alpha, ALPHA_THREAD, project_a)
    append(transport, 'pending')
    core.collect()
    project_a.rmdir()
    assert relay.next_event() is None
    assert relay.status(alpha)['bindings'][0]['eligible'] is False


def test_project_replaced_by_symlink_is_not_dispatched(env):
    core, transport, _, relay, alpha, _, project_a, project_b = env
    relay.bind(alpha, ALPHA_THREAD, project_a)
    append(transport, 'pending')
    core.collect()
    project_a.rmdir()
    project_a.symlink_to(project_b, target_is_directory=True)
    assert relay.next_event() is None
    assert relay.status(alpha)['bindings'][0]['project_available'] is False


@pytest.mark.parametrize('failure', ['false', 'exception', 'non_boolean'])
def test_project_validator_drift_preserves_pending_work_until_restored(env, failure):
    core, transport, clock, _, alpha, _, project_a, _ = env
    control = {'valid': True}

    def validate(binding):
        assert binding['client_id'] == alpha
        assert binding['thread_id'] == ALPHA_THREAD
        assert binding['project_dir'] == str(project_a)
        if control['valid']:
            return True
        if failure == 'exception':
            raise OSError('profile unavailable')
        return False if failure == 'false' else 'truthy but invalid'

    relay = RelayStore(core, project_validator=validate)
    relay.bind(alpha, ALPHA_THREAD, project_a)
    append(transport, 'pending')
    core.collect()
    control['valid'] = False
    assert relay.next_event() is None
    binding = relay.status(alpha)['bindings'][0]
    assert binding['project_available'] is True
    assert binding['profile_valid'] is False
    assert binding['eligible'] is False
    assert binding['notifications'] == {}
    control['valid'] = True
    event = relay.next_event()
    relay.resolve(event['notification_id'], 'failed')
    clock.advance(1)
    control['valid'] = False
    assert relay.next_event() is None
    assert relay.status(alpha)['notifications'][0]['status'] == 'retry'
    control['valid'] = True
    assert relay.next_event()['attempt'] == 2
