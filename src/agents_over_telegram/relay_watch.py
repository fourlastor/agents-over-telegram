"""Bounded local event waiting; the owning Codex chat performs host handoff.

This process never calls Codex or sends Telegram replies. A quiet watcher is not
an unsolicited host notification: its owner must still be waiting on its output.
"""
from __future__ import annotations

import json
import math
import os
import queue
import re
import selectors
import signal
import sys
import termios
import threading
import time

from .core import CoreError


def _code(error):
    code = str(getattr(error, 'code', 'relay_collection_failed'))
    return code if re.fullmatch(r'[a-z0-9_]{1,64}', code) else 'relay_collection_failed'


def watch(core, store, *, once=False, duration=55, interval=1.5, collect=False,
          input_stream=None, output_stream=None):
    """Wait for metadata-only events, without claiming inbox processing leases.

    --once is intended for bounded shell tools: completion returns promptly on
    an event. Persistent mode accepts JSONL receipts on attached stdin. The
    duration is always bounded because a shell child cannot observe chat end.
    """
    if (type(once) is not bool or type(collect) is not bool
            or isinstance(duration, bool) or not isinstance(duration, (int, float))
            or not math.isfinite(duration) or not 0 < duration <= 3600
            or isinstance(interval, bool) or not isinstance(interval, (int, float))
            or not math.isfinite(interval) or not .1 <= interval <= 60):
        raise CoreError('invalid_relay_watch_options')
    input_stream = sys.stdin if input_stream is None else input_stream
    output_stream = sys.stdout if output_stream is None else output_stream
    stop = threading.Event()
    errors = queue.Queue(maxsize=1)
    deadline = time.monotonic() + duration
    worker = None
    selector = None
    terminal = None
    old_handlers = {}
    reason = 'timeout'
    emitted_once = False

    def emit(value):
        output_stream.write(json.dumps(value, separators=(',', ':'), ensure_ascii=True) + '\n')
        output_stream.flush()

    def collect_loop():
        backoff = interval
        last_error = None
        while not stop.is_set():
            try:
                core.collect(timeout=10)
                backoff, last_error = interval, None
            except Exception as error:
                code = _code(error)
                if code != last_error:
                    try:
                        errors.put_nowait(code)
                    except queue.Full:
                        pass
                    last_error = code
                backoff = min(60, max(interval, backoff * 2))
            stop.wait(backoff)

    def signal_stop(signum, frame):
        stop.set()

    with core._lock('relay-watcher') as acquired:
        if not acquired:
            raise CoreError('relay_watcher_busy')
        try:
            if not once:
                # select() also handles closed/default stdin and regular files;
                # Linux epoll refuses those before we can observe EOF.
                selector = selectors.SelectSelector()
                fd = input_stream.fileno()
                selector.register(fd, selectors.EVENT_READ)
                if os.isatty(fd):
                    terminal = (fd, termios.tcgetattr(fd))
                    settings = termios.tcgetattr(fd)
                    settings[3] &= ~termios.ECHO
                    termios.tcsetattr(fd, termios.TCSANOW, settings)
            if threading.current_thread() is threading.main_thread():
                for signum in (signal.SIGTERM, signal.SIGINT):
                    old_handlers[signum] = signal.signal(signum, signal_stop)
            recovered = store.recover_inflight()
            emit({'type': 'relay_ready', 'once': once, 'duration': duration,
                  'collect': collect, 'recovered': recovered})
            buffer = b''
            next_scan = 0.0
            while not stop.is_set() and time.monotonic() < deadline:
                # Read shutdown/receipts before emitting any more work.
                if selector is not None:
                    for key, _ in selector.select(timeout=0):
                        data = os.read(key.fd, 4096)
                        if not data:
                            reason = 'stdin_closed'
                            stop.set()
                            break
                        buffer += data
                        if len(buffer) > 16384:
                            raise CoreError('invalid_relay_control')
                        while b'\n' in buffer:
                            line, buffer = buffer.split(b'\n', 1)
                            try:
                                command = json.loads(line)
                                if not isinstance(command, dict):
                                    raise ValueError()
                                if command == {'command': 'stop'}:
                                    reason = 'requested'
                                    stop.set()
                                    break
                                if (set(command) != {'command', 'notification_id', 'outcome'}
                                        or command['command'] != 'resolve'):
                                    raise ValueError()
                                receipt = store.resolve(command['notification_id'], command['outcome'])
                                emit({'type': 'relay_receipt', **receipt})
                            except (ValueError, TypeError, UnicodeError, CoreError) as error:
                                emit({'type': 'relay_error', 'error': _code(error) if isinstance(error, CoreError)
                                      else 'invalid_relay_control'})
                if stop.is_set():
                    break
                if collect and worker is None:
                    worker = threading.Thread(target=collect_loop, name='aot-relay-collector', daemon=True)
                    worker.start()
                try:
                    error = errors.get_nowait()
                except queue.Empty:
                    pass
                else:
                    emit({'type': 'relay_error', 'error': error})
                now = time.monotonic()
                if now >= next_scan:
                    event = store.next_event()
                    if event is not None:
                        emit(event)
                        if once:
                            emitted_once = True
                            reason = 'event'
                            break
                    next_scan = now + min(interval, .5)
                stop.wait(min(.1, max(0, deadline - time.monotonic())))
            if stop.is_set() and reason == 'timeout':
                reason = 'signal'
        finally:
            stop.set()
            # Network intake is a daemon thread with no sending operations. The
            # CLI exits after this call; a pending read cannot keep it alive.
            if worker is not None:
                worker.join(timeout=.2)
            if not emitted_once:
                store.recover_inflight()
            if selector is not None:
                selector.close()
            if terminal is not None:
                termios.tcsetattr(terminal[0], termios.TCSANOW, terminal[1])
            for signum, handler in old_handlers.items():
                signal.signal(signum, handler)
        emit({'type': 'relay_stopped', 'reason': reason})

