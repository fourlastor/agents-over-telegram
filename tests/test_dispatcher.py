"""Direct dispatcher contract with deterministic app-server responses.

Socket framing/auth/protocol behavior is covered by test_appserver_client; these
tests focus on routing, durable handoff semantics and preserved Telegram state.
"""
import copy
import json
import os
from pathlib import Path
import subprocess
import sys
import threading
import time

import pytest

from agents_over_telegram import dispatcher
from agents_over_telegram.appserver_client import AppServerError
from agents_over_telegram.core import CoreError
from agents_over_telegram.relay import WAKE_NOTICE
from test_relay import ALPHA_THREAD, BETA_THREAD, append, env, protected_snapshot


TURN_ID = '01a15453-3dc7-73e3-b263-0dc1d8de7afd'


@pytest.fixture
def server(env, monkeypatch):
    _, _, _, _, _, _, project_a, project_b = env
    state = {'calls': [], 'connections': [], 'events': [], 'connect_failures': [],
             'threads': {ALPHA_THREAD: str(project_a), BETA_THREAD: str(project_b)},
             'on_request': None, 'on_drain': None, 'start_error': None,
             'start_result': {'turn': {'id': TURN_ID, 'status': 'inProgress'}}}

    class Connection:
        def __init__(self, endpoint, *, token_file=None):
            self.closed = False
            state['connections'].append(self)
            assert endpoint == 'unix:///explicit-test.sock'

        def connect(self):
            if state['connect_failures']:
                raise state['connect_failures'].pop(0)
            return self

        def close(self):
            self.closed = True

        def request(self, method, params, *, mutating=False):
            state['calls'].append((method, copy.deepcopy(params), mutating))
            if state['on_request']:
                result = state['on_request'](method, params)
                if result is not None:
                    return result
            if method in ('thread/read', 'thread/resume'):
                result = {'thread': {'id': params['threadId'], 'cwd': state['threads'][params['threadId']]}}
                if method == 'thread/resume':
                    result['cwd'] = state['threads'][params['threadId']]
                return result
            assert method == 'turn/start' and mutating is True
            if state['start_error']:
                raise state['start_error']
            return copy.deepcopy(state['start_result'])

        def drain(self, timeout=0):
            if state['on_drain']:
                state['on_drain']()
            events, state['events'] = state['events'], []
            if not events:
                time.sleep(min(timeout, .005))
            return events

    monkeypatch.setattr(dispatcher, 'AppServerConnection', Connection)
    return state


def ready(env):
    core, transport, _, store, alpha, _, project_a, _ = env
    store.bind(alpha, ALPHA_THREAD, project_a)
    append(transport, 'Secret Telegram content, never forwarded as a prompt')
    core.collect()
    return core, store, alpha


def run(env, **kwargs):
    core, _, _, store, *_ = env
    events = []
    result = dispatcher.run_dispatcher(core, store, 'unix:///explicit-test.sock',
                                       duration=.04, interval=.01, emit=events.append, **kwargs)
    return result, events


def starts(server):
    return [call for call in server['calls'] if call[0] == 'turn/start']


def test_acceptance_fixed_tool_output_no_override_no_delivery_side_effects(env, server):
    core, store, alpha = ready(env)
    core.send(alpha, 'outgoing remains queued', 'dispatcher-test-outgoing')
    before = protected_snapshot(core)
    result, events = run(env)
    assert result['accepted'] == 1
    assert len(server['connections']) == 1
    assert server['connections'][0].closed
    assert [call[0] for call in server['calls']] == ['thread/read', 'thread/resume', 'turn/start']
    _, params, mutating = starts(server)[0]
    assert mutating is True
    assert set(params) == {'threadId', 'input', 'toolOutput', 'turnTrigger'}
    assert params['threadId'] == ALPHA_THREAD
    assert params['input'] == [] and params['turnTrigger'] == 'telegram'
    output = params['toolOutput']
    assert output['name'] == 'telegram_inbox_ready'
    assert output['namespace'] == 'agents_over_telegram' and output['retain'] is True
    body = json.loads(output['output'])
    assert body['notice'] == WAKE_NOTICE
    assert set(body) == {'notification_id', 'notice'}
    assert 'Secret Telegram' not in json.dumps(server['calls'])
    receipt = next(event for event in events if event['type'] == 'dispatcher_receipt')
    assert receipt['status'] == 'accepted' and receipt['turn_id'] == TURN_ID
    assert protected_snapshot(core) == before
    assert store.status(alpha)['bindings'][0]['dispatchable_messages'] == 0


@pytest.mark.parametrize('method,field,value', [
    ('thread/read', 'id', BETA_THREAD), ('thread/read', 'cwd', '/tmp'),
    ('thread/resume', 'id', BETA_THREAD), ('thread/resume', 'cwd', '/tmp'),
])
def test_wrong_target_or_cwd_never_claims_or_starts(env, server, method, field, value):
    _, store, alpha = ready(env)
    def mismatch(current, params):
        if current == method:
            thread = {'id': ALPHA_THREAD, 'cwd': server['threads'][ALPHA_THREAD]}
            thread[field] = value
            return {'thread': thread}
    server['on_request'] = mismatch
    result, events = run(env)
    assert result['accepted'] == 0 and starts(server) == []
    assert store.status(alpha)['bindings'][0]['notifications'] == {}
    assert sum(event['type'] == 'dispatcher_route_blocked' for event in events) == 1


@pytest.mark.parametrize('block', ['paused', 'profile_drift'])
def test_pause_or_profile_drift_during_preflight_prevents_claim(env, server, block):
    core, store, alpha = ready(env)
    def change(method, params):
        if method == 'thread/resume':
            if block == 'paused':
                core.pause(alpha)
            else:
                store.project_validator = lambda binding: False
    server['on_request'] = change
    run(env)
    assert starts(server) == []
    assert store.status(alpha)['bindings'][0]['notifications'] == {}


def test_paused_before_start_has_no_thread_preflight(env, server):
    core, store, alpha = ready(env)
    core.pause(alpha)
    run(env)
    assert server['calls'] == []
    assert store.status(alpha)['bindings'][0]['notifications'] == {}


@pytest.mark.parametrize('failure', ['timeout', 'disconnect', 'malformed', 'bad_status'])
def test_ambiguous_submission_is_held_across_restart_without_repeat(env, server, failure):
    core, store, alpha = ready(env)
    before = protected_snapshot(core)
    if failure in ('malformed', 'bad_status'):
        server['start_result'] = {} if failure == 'malformed' else {'turn': {'id': TURN_ID, 'status': 'made-up'}}
    else:
        server['start_error'] = AppServerError('appserver_' + failure, ambiguous=True)
    result, _ = run(env)
    assert result['uncertain'] == 1 and result['reason'] == 'connection_failed'
    assert store.status(alpha)['notifications'][0]['status'] == 'uncertain'
    assert protected_snapshot(core) == before
    count = len(server['calls'])
    run(env)
    assert len(server['calls']) == count
    assert len(starts(server)) == 1


def test_explicit_rpc_rejection_is_definite_failure_with_bounded_retry(env, server):
    _, store, alpha = ready(env)
    server['start_error'] = AppServerError('appserver_rpc_error', rpc_code=-32602)
    result, events = run(env)
    assert result['failed'] == 1 and result['uncertain'] == 0
    assert len(starts(server)) == 1
    notification = store.status(alpha)['notifications'][0]
    assert notification['status'] == 'retry'
    assert notification['next_attempt_at'] > 1000
    assert next(event for event in events if event['type'] == 'dispatcher_receipt')['outcome'] == 'failed'


def test_startup_recovery_does_not_resend_previous_inflight(env, server):
    _, store, alpha = ready(env)
    original = store.next_event()
    run(env)
    assert server['calls'] == []
    state = store.status(alpha)['notifications'][0]
    assert state['notification_id'] == original['notification_id'] and state['status'] == 'uncertain'


def test_busy_thread_can_accept_second_signal_in_same_turn(env, server):
    core, transport, _, store, alpha, *_ = env
    ready(env)
    events = []
    def emit(event):
        events.append(event)
        if event['type'] == 'dispatcher_receipt' and len(starts(server)) == 1:
            append(transport, 'second arrival while target busy')
            core.collect()
    result = dispatcher.run_dispatcher(core, store, 'unix:///explicit-test.sock',
                                       duration=.05, interval=.01, emit=emit)
    assert result['accepted'] == 2
    receipts = [event for event in events if event['type'] == 'dispatcher_receipt']
    assert [event['turn_id'] for event in receipts] == [TURN_ID, TURN_ID]
    assert receipts[0]['notification_id'] != receipts[1]['notification_id']
    assert core.connection_status(alpha)['pending_messages'] == 2


def test_server_requests_are_metadata_only_and_never_answered(env, server):
    ready(env)
    server['events'] = [{'kind': 'server_request', 'method': 'item/commandExecution/requestApproval',
                         'threadId': ALPHA_THREAD, 'turnId': TURN_ID, 'payload': 'secret command'},
                        {'kind': 'notification', 'method': 'turn/completed', 'turnId': TURN_ID}]
    result, events = run(env)
    assert result['needs_attention'] == 1
    attention = next(event for event in events if event['type'] == 'dispatcher_needs_attention')
    assert 'payload' not in attention and 'secret command' not in json.dumps(events)
    assert [call[0] for call in server['calls']] == ['thread/read', 'thread/resume', 'turn/start']


def test_quiet_connection_drains_without_periodic_model_turns(env, server):
    _, _, _, store, alpha, _, project_a, _ = env
    store.bind(alpha, ALPHA_THREAD, project_a)
    drained = []
    server['on_drain'] = lambda: drained.append(True)
    run(env)
    assert len(drained) > 1
    assert server['calls'] == []


def test_initial_endpoint_readiness_retry_before_any_claim(env, server):
    _, store, alpha = ready(env)
    server['connect_failures'] = [AppServerError('appserver_socket_unavailable')]
    events = []
    result = dispatcher.run_dispatcher(env[0], store, 'unix:///explicit-test.sock',
                                       duration=.2, interval=.01, emit=events.append)
    assert result['accepted'] == 1
    assert len(server['connections']) == 2
    assert all(connection.closed for connection in server['connections'])
    assert store.status(alpha)['bindings'][0]['notifications'] == {'accepted': 1}


def test_unsafe_endpoint_fails_without_claim_or_fallback(env, server):
    _, store, alpha = ready(env)
    server['connect_failures'] = [AppServerError('unsafe_appserver_socket')]
    result, _ = run(env)
    assert result['reason'] == 'connection_failed'
    assert len(server['connections']) == 1 and server['calls'] == []
    assert store.status(alpha)['bindings'][0]['notifications'] == {}


def test_shared_watcher_lock_blocks_dispatcher(env, server):
    core, _, _ = ready(env)
    with core._lock('relay-watcher') as acquired:
        assert acquired
        with pytest.raises(CoreError, match='relay_watcher_busy'):
            run(env)
    assert server['connections'] == []


def test_indefinite_dispatcher_stops_without_interrupting_target(env, server):
    core, store, _ = ready(env)
    stop = threading.Event()
    events = []
    def emit(event):
        events.append(event)
        if event['type'] == 'dispatcher_receipt':
            stop.set()
    result = dispatcher.run_dispatcher(core, store, 'unix:///explicit-test.sock',
                                       duration=0, interval=.01, emit=emit, stop_event=stop)
    assert result['reason'] == 'stopped' and result['accepted'] == 1
    assert 'turn/interrupt' not in [call[0] for call in server['calls']]


def test_client_filter_skips_unvalidated_target_without_claim(env):
    core, transport, _, store, alpha, beta, project_a, project_b = env
    store.bind(alpha, ALPHA_THREAD, project_a)
    store.bind(beta, BETA_THREAD, project_b)
    append(transport, 'alpha')
    append(transport, 'beta', -200)
    core.collect()
    assert store.next_event(allowed_client_ids=set()) is None
    assert store.next_event(allowed_client_ids={beta})['client_id'] == beta
    assert store.status(alpha)['bindings'][0]['notifications'] == {}


def test_resumed_effective_cwd_must_match_not_only_thread_metadata(env, server):
    _, store, alpha = ready(env)
    def mismatch(method, params):
        if method == 'thread/resume':
            return {'thread': {'id': ALPHA_THREAD, 'cwd': server['threads'][ALPHA_THREAD]}, 'cwd': '/tmp'}
    server['on_request'] = mismatch
    run(env)
    assert starts(server) == []
    assert store.status(alpha)['bindings'][0]['notifications'] == {}


@pytest.mark.parametrize('method', ['thread/read', 'thread/resume'])
def test_explicit_readonly_thread_never_claims_notification(env, server, method):
    _, store, alpha = ready(env)
    def readonly(current, params):
        if current == method:
            return {'thread': {'id': ALPHA_THREAD, 'cwd': server['threads'][ALPHA_THREAD],
                               'canAcceptDirectInput': False}, 'cwd': server['threads'][ALPHA_THREAD]}
    server['on_request'] = readonly
    run(env)
    assert starts(server) == []
    assert store.status(alpha)['bindings'][0]['notifications'] == {}


def test_unspecified_direct_input_capability_remains_compatible(env, server):
    ready(env)
    def unknown_capability(method, params):
        if method in ('thread/read', 'thread/resume'):
            return {'thread': {'id': ALPHA_THREAD, 'cwd': server['threads'][ALPHA_THREAD],
                               'canAcceptDirectInput': None}, 'cwd': server['threads'][ALPHA_THREAD]}
    server['on_request'] = unknown_capability
    result, _ = run(env)
    assert result['accepted'] == 1


def test_runtime_rpc_error_is_uncertain_and_does_not_repeat(env, server):
    _, store, alpha = ready(env)
    server['start_error'] = AppServerError('appserver_rpc_error', ambiguous=True, rpc_code=-32603)
    result, _ = run(env)
    assert result['uncertain'] == 1 and result['reason'] == 'duration'
    assert store.status(alpha)['notifications'][0]['status'] == 'uncertain'
    run(env)
    assert len(starts(server)) == 1


def test_retry_handled_elsewhere_is_cleaned_without_model_turn(env, server):
    core, _, clock, store, alpha, *_ = env
    ready(env)
    event = store.next_event()
    store.resolve(event['notification_id'], 'failed')
    deliveries = core.poll(alpha)
    core.ack(alpha, [item['delivery_id'] for item in deliveries['deliveries']], deliveries['lease_token'])
    clock.advance(1)
    run(env)
    assert starts(server) == []
    assert store.status(alpha)['notifications'] == []
    assert [call[0] for call in server['calls']] == ['thread/read', 'thread/resume']


def test_target_change_after_preflight_never_dispatches(env, server, monkeypatch):
    _, store, alpha = ready(env)
    original = store.next_event
    def changed(**kwargs):
        event = original(**kwargs)
        if event:
            event['thread_id'] = BETA_THREAD
        return event
    monkeypatch.setattr(store, 'next_event', changed)
    result, events = run(env)
    assert result['failed'] == 1 and starts(server) == []
    assert store.status(alpha)['notifications'][0]['status'] == 'retry'
    assert any(event.get('error') == 'dispatcher_target_changed' for event in events)


def test_invalid_route_preflight_uses_bounded_exponential_backoff(env, server, monkeypatch):
    core, store, _ = ready(env)
    timer = {'now': 0}
    attempts = []
    monkeypatch.setattr(dispatcher.time, 'monotonic', lambda: timer['now'])
    def mismatch(method, params):
        attempts.append(timer['now'])
        return {'thread': {'id': BETA_THREAD, 'cwd': '/tmp'}}
    def advance():
        timer['now'] += 1
    server['on_request'] = mismatch
    server['on_drain'] = advance
    dispatcher.run_dispatcher(core, store, 'unix:///explicit-test.sock',
                              duration=130, interval=.01, emit=lambda event: None)
    assert attempts == [0, 1, 3, 7, 15, 31, 63, 123]
    assert starts(server) == []


@pytest.mark.parametrize('endpoint', ['https://secret-token@external.example/path', 'unix:///tmp/aot-socket-that-does-not-exist'])
def test_cli_startup_failure_is_nonzero_safe_and_preserves_work(env, endpoint):
    core, _, alpha = ready(env)
    core.send(alpha, 'outgoing remains queued', 'cli-dispatcher-test')
    before = protected_snapshot(core)
    environment = dict(os.environ, PYTHONPATH=str(Path(__file__).resolve().parents[1] / 'src'))
    result = subprocess.run([sys.executable, '-m', 'agents_over_telegram', '--state-dir',
                             str(core.state_dir), '--fake', 'relay-dispatch', '--endpoint', endpoint,
                             '--duration', '.02'], capture_output=True, text=True, env=environment, timeout=10)
    assert result.returncode == 1
    assert json.loads(result.stderr)['error'] == 'dispatcher_connection_failed'
    assert 'secret-token' not in result.stdout + result.stderr
    assert 'Traceback' not in result.stdout + result.stderr
    assert protected_snapshot(core) == before


def test_cli_passes_dispatch_options_and_disables_send_recovery(env, monkeypatch):
    import agents_over_telegram.core as core_module
    from agents_over_telegram.cli import parser, run as run_cli
    core = env[0]
    observed = {}
    original_core = core_module.Core
    def create_core(*args, **kwargs):
        observed['recover_sends'] = kwargs['recover_sends']
        return original_core(*args, **kwargs)
    def dispatch_cli(actual_core, store, endpoint, **kwargs):
        observed.update(kwargs)
        observed['endpoint'] = endpoint
        assert actual_core.database == core.database
        assert store.project_validator is not None
        return {'reason': 'duration'}
    monkeypatch.setattr(core_module, 'Core', create_core)
    monkeypatch.setattr(dispatcher, 'run_dispatcher', dispatch_cli)
    options = parser().parse_args(['--state-dir', str(core.state_dir), '--fake', 'relay-dispatch',
        '--endpoint', 'ws://127.0.0.1:12345', '--token-file', '/tmp/private-token.json',
        '--collect', '--duration', '0'])
    assert run_cli(options) is None
    assert observed == {'recover_sends': False, 'endpoint': 'ws://127.0.0.1:12345',
                       'token_file': Path('/tmp/private-token.json'), 'collect': True, 'duration': 0}
