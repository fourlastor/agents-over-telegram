"""Persistent local Codex dispatcher: durable inbox signals, never inbox ACKs.

The explicitly supplied endpoint must be the desktop's shared backend. This
module never starts a backend, answers server requests, supplies turn settings,
or substitutes another connection after an ambiguous mutation.
"""
from __future__ import annotations

import json
import math
from pathlib import Path
import queue
import re
import signal
import threading
import time
import uuid

from .appserver_client import AppServerConnection, AppServerError
from .core import CoreError
from .relay import WAKE_NOTICE


def _error_code(error):
    value = getattr(error, 'code', '')
    return value if isinstance(value, str) and re.fullmatch(r'[a-z0-9_]{1,64}', value) else 'dispatcher_failed'


def _uuid(value):
    try:
        return isinstance(value, str) and str(uuid.UUID(value)) == value
    except (ValueError, TypeError, AttributeError):
        return False


def _matches_thread(response, binding, *, resumed=False):
    thread = response.get('thread') if isinstance(response, dict) else None
    if not isinstance(thread, dict) or thread.get('id') != binding['thread_id']:
        return False
    if thread.get('canAcceptDirectInput') is False:
        return False
    try:
        cwd = thread.get('cwd')
        captured_matches = (isinstance(cwd, str) and Path(cwd).is_absolute()
                and Path(cwd).is_dir() and str(Path(cwd).resolve(strict=True)) == binding['project_dir'])
        if not captured_matches or not resumed:
            return captured_matches
        effective_cwd = response.get('cwd')
        return (isinstance(effective_cwd, str) and Path(effective_cwd).is_absolute()
                and Path(effective_cwd).is_dir()
                and str(Path(effective_cwd).resolve(strict=True)) == binding['project_dir'])
    except (OSError, ValueError, RuntimeError):
        return False


def run_dispatcher(core, store, endpoint, *, token_file=None, collect=False,
                   duration=55, emit=None, interval=.5, stop_event=None):
    """Dispatch fixed tool output to the existing thread through one connection.

    ``duration=0`` runs until stopped by the owning app or a signal. Quiet waits
    only drain protocol events and inspect the local durable inbox. They never
    create periodic model turns. ``accepted`` is a host receipt, not proof of
    completed processing or a Telegram reply. Callers construct Core with
    ``recover_sends=False``; this dispatcher never flushes outgoing messages.
    """
    if (type(collect) is not bool or isinstance(duration, bool)
            or not isinstance(duration, (int, float)) or not math.isfinite(duration)
            or not 0 <= duration <= 86400 or isinstance(interval, bool)
            or not isinstance(interval, (int, float)) or not math.isfinite(interval)
            or not .01 <= interval <= 60):
        raise CoreError('invalid_dispatcher_options')
    if emit is None:
        def emit(value):
            print(json.dumps(value, separators=(',', ':'), ensure_ascii=True), flush=True)
    stop = threading.Event() if stop_event is None else stop_event
    deadline = time.monotonic() + duration if duration else float('inf')
    connection = None
    collector = None
    errors = queue.Queue(maxsize=1)
    handlers = {}
    counters = {'accepted': 0, 'failed': 0, 'uncertain': 0, 'needs_attention': 0}
    reason = 'duration'
    last_route_errors = {}
    route_backoff = {}

    def signal_stop(_signum, _frame):
        stop.set()

    def collect_loop():
        backoff = interval
        last_error = None
        while not stop.is_set():
            try:
                core.collect(timeout=10)
                backoff, last_error = interval, None
            except Exception as error:
                code = _error_code(error)
                if code != last_error:
                    try:
                        errors.put_nowait(code)
                    except queue.Full:
                        pass
                    last_error = code
                backoff = min(60, max(interval, backoff * 2))
            stop.wait(backoff)

    def drain(timeout=0):
        for event in connection.drain(timeout=timeout):
            # The transport already strips payloads. Keep a second allowlist
            # here so future client fields cannot leak through diagnostics.
            metadata = {key: event[key] for key in ('kind', 'method', 'threadId', 'turnId') if key in event}
            attention = event.get('kind') == 'server_request'
            if attention:
                counters['needs_attention'] += 1
            emit({'type': 'dispatcher_needs_attention' if attention else 'dispatcher_event', **metadata})

    with core._lock('relay-watcher') as acquired:
        if not acquired:
            raise CoreError('relay_watcher_busy')
        try:
            if threading.current_thread() is threading.main_thread():
                for signum in (signal.SIGTERM, signal.SIGINT):
                    handlers[signum] = signal.signal(signum, signal_stop)
            recovered = store.recover_inflight()
            # Retry only initial readiness failures, before any event is claimed.
            # Each candidate is a new connection; a used connection never retries.
            for attempt in range(5):
                if stop.is_set() or time.monotonic() >= deadline:
                    break
                candidate = AppServerConnection(endpoint, token_file=token_file)
                try:
                    candidate.connect()
                    connection = candidate
                    break
                except AppServerError as error:
                    candidate.close()
                    if (error.code not in ('appserver_connect_failed', 'appserver_socket_unavailable',
                                          'appserver_timeout', 'appserver_transport_failed') or attempt == 4):
                        raise
                    emit({'type': 'dispatcher_connect_wait', 'error': error.code, 'attempt': attempt + 1})
                    stop.wait(min(.1 * 2 ** attempt, max(0, deadline - time.monotonic())))
            if connection is None:
                reason = 'stopped' if stop.is_set() else 'connection_failed'
            else:
                emit({'type': 'dispatcher_ready', 'duration': duration, 'collect': collect, 'recovered': recovered})
                if collect:
                    collector = threading.Thread(target=collect_loop, name='aot-dispatcher-collector', daemon=True)
                    collector.start()
            next_scan = 0.0
            while connection is not None and not stop.is_set() and time.monotonic() < deadline:
                try:
                    code = errors.get_nowait()
                except queue.Empty:
                    pass
                else:
                    emit({'type': 'dispatcher_collection_error', 'error': code})
                if time.monotonic() >= next_scan:
                    allowed = {}
                    for binding in store.status()['bindings']:
                        client_id = binding['client_id']
                        identity = (binding['thread_id'], binding['project_dir'])
                        previous = route_backoff.get(client_id)
                        if previous and (previous[0] != identity or not binding['eligible']):
                            route_backoff.pop(client_id, None)
                            last_route_errors.pop(client_id, None)
                            previous = None
                        if not binding['eligible'] or (not binding['dispatchable_messages']
                                                       and binding['retry_pending_messages'] != 0):
                            continue
                        if previous and time.monotonic() < previous[2]:
                            continue
                        try:
                            result = connection.request('thread/read', {
                                'threadId': binding['thread_id'], 'includeTurns': False})
                            if not _matches_thread(result, binding):
                                raise CoreError('dispatcher_target_mismatch')
                            result = connection.request('thread/resume', {
                                'threadId': binding['thread_id'], 'excludeTurns': True})
                            if not _matches_thread(result, binding, resumed=True):
                                raise CoreError('dispatcher_target_mismatch')
                        except (CoreError, AppServerError) as error:
                            if isinstance(error, AppServerError) and error.code != 'appserver_rpc_error':
                                raise
                            code = _error_code(error)
                            attempts = min(7, previous[1] + 1) if previous else 1
                            route_backoff[client_id] = (identity, attempts,
                                time.monotonic() + min(60, 2 ** (attempts - 1)))
                            if last_route_errors.get(client_id) != code:
                                emit({'type': 'dispatcher_route_blocked', 'client_id': client_id,
                                      'thread_id': binding['thread_id'], 'error': code})
                                last_route_errors[client_id] = code
                            continue
                        last_route_errors.pop(client_id, None)
                        route_backoff.pop(client_id, None)
                        allowed[client_id] = (binding['thread_id'], binding['project_dir'])
                    # The store checks pause, leases and project validity again
                    # in the same transaction that records the durable intent.
                    if stop.is_set():
                        break
                    event = store.next_event(allowed_client_ids=set(allowed))
                    if event is not None:
                        if stop.is_set():
                            store.resolve(event['notification_id'], 'failed')
                            break
                        if allowed.get(event['client_id']) != (event['thread_id'], event['project_dir']):
                            receipt = store.resolve(event['notification_id'], 'failed')
                            counters['failed'] += 1
                            emit({'type': 'dispatcher_receipt', 'client_id': event['client_id'],
                                  'thread_id': event['thread_id'], 'outcome': 'failed',
                                  'error': 'dispatcher_target_changed', **receipt})
                            next_scan = time.monotonic() + interval
                            continue
                        params = {'threadId': event['thread_id'], 'input': [], 'toolOutput': {
                            'name': 'telegram_inbox_ready', 'namespace': 'agents_over_telegram',
                            'output': json.dumps({'notification_id': event['notification_id'], 'notice': WAKE_NOTICE},
                                                 separators=(',', ':'), ensure_ascii=True),
                            'retain': True}, 'turnTrigger': 'telegram'}
                        try:
                            response = connection.request('turn/start', params, mutating=True)
                            turn = response.get('turn') if isinstance(response, dict) else None
                            if (not isinstance(turn, dict) or not _uuid(turn.get('id'))
                                    or turn.get('status') not in ('inProgress', 'completed', 'interrupted', 'failed')):
                                raise AppServerError('invalid_appserver_turn_response', ambiguous=True)
                        except AppServerError as error:
                            outcome = 'uncertain' if error.ambiguous else 'failed'
                            receipt = store.resolve(event['notification_id'], outcome)
                            counters[outcome] += 1
                            emit({'type': 'dispatcher_receipt', 'client_id': event['client_id'],
                                  'thread_id': event['thread_id'], 'outcome': outcome,
                                  'error': _error_code(error), **receipt})
                            if error.code != 'appserver_rpc_error':
                                raise
                        else:
                            receipt = store.resolve(event['notification_id'], 'accepted')
                            counters['accepted'] += 1
                            emit({'type': 'dispatcher_receipt', 'client_id': event['client_id'],
                                  'thread_id': event['thread_id'], 'turn_id': turn['id'],
                                  'outcome': 'accepted', **receipt})
                    next_scan = time.monotonic() + interval
                drain(timeout=min(.1, max(0, deadline - time.monotonic())))
            if stop.is_set():
                reason = 'stopped'
        except KeyboardInterrupt:
            reason = 'stopped'
            stop.set()
        except AppServerError as error:
            reason = 'connection_failed'
            emit({'type': 'dispatcher_error', 'error': _error_code(error)})
        finally:
            stop.set()
            if connection is not None:
                connection.close()
            if collector is not None:
                collector.join(timeout=.2)
            store.recover_inflight()
            for signum, handler in handlers.items():
                signal.signal(signum, handler)
        result = {'type': 'dispatcher_stopped', 'reason': reason, **counters}
        emit(result)
        return result
