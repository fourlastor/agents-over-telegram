"""Wake local chats in an already running desktop through its native queue.

The CLI records a fixed USER-role note. The OS URL contains only the bound chat
UUID. No model text, Telegram contents, credentials, app launch configuration,
or backend protocol are synthesized. OS URL handling still has an exit race:
if the desktop exits after our final process check, its handler may reopen it.
"""
from __future__ import annotations

import errno
import json
import math
import os
from pathlib import Path
import plistlib
import queue
import re
import signal
import sqlite3
import stat
import subprocess
import sys
import threading
import time
import uuid

from .core import CoreError
from .relay import WAKE_NOTICE


def default_codex_binary():
    if sys.platform == 'darwin':
        for root in (Path('/Applications'), Path.home() / 'Applications'):
            for name in ('ChatGPT.app', 'Codex.app'):
                resources = root / name / 'Contents' / 'Resources'
                for binary in (resources / 'codex-cli/CodexCLI.app/Contents/MacOS/codex',
                               resources / 'codex'):
                    if binary.is_file():
                        return binary
        return Path('/Applications/ChatGPT.app/Contents/Resources/codex-cli/CodexCLI.app/Contents/MacOS/codex')
    return Path('/usr/lib/chatgpt/resources/codex')


DEFAULT_CODEX_BINARY = default_codex_binary()
PROC_ROOT = Path('/proc')
# How often the listener looks for long-held unresolved wakes, and how often it
# repeats the warning for the same notification while it stays unresolved. The
# repeat state lives in memory, so a restarted listener warns again once.
STUCK_WAKE_CHECK_SECONDS = 60
# An enqueue whose outcome is unknown is checked against the desktop's own
# records once its child process has surely ended (it is killed after 30 s).
UNKNOWN_ENQUEUE_CHECK_AFTER_SECONDS = 120
# A chat whose latest turn is in progress and showed activity this recently is
# busy: AOT's Codex hooks deliver the notice inside that turn, so the listener
# does not also queue one. An older in-progress turn is treated as stale (for
# example left open by a crash), and the listener queues as usual.
BUSY_TURN_ACTIVITY_SECONDS = 600
STUCK_WAKE_WARNING_REPEAT_SECONDS = 3600
STUCK_WAKE_MESSAGES = {
    'review_uncertain_wake': (
        'Unresolved wake notification blocks every later wake for this client. Reconcile it against '
        'the target chat, then record the outcome with relay-resolve. A running desktop listener '
        'settles an unknown enqueue by itself when the desktop records show its outcome.'),
    'wait_for_desktop_wake': (
        'Queued wake has not opened in the desktop yet, so later wakes for this client wait. The '
        'listener retries only its wake URL while the desktop is open and the connection is active. '
        'Do not resolve it by hand: that stops the retries.'),
    'review_failing_wake': (
        'Queued wake URL keeps failing while the desktop is open and the connection is active, so later '
        'wakes for this client wait. Check that the target chat still exists and that its wake URL opens. '
        'Opening that chat in the desktop app delivers the queued notice; then record accepted with '
        'relay-resolve, or cancelled to dismiss it. The listener keeps retrying until then.'),
}

def _uuid(value):
    try:
        return isinstance(value, str) and str(uuid.UUID(value)) == value
    except (ValueError, TypeError, AttributeError):
        return False


def _code(error):
    value = getattr(error, 'code', '')
    return value if isinstance(value, str) and re.fullmatch(r'[a-z0-9_]{1,64}', value) else 'desktop_dispatcher_failed'


class DesktopQueueError(CoreError):
    def __init__(self, code, *, ambiguous=False):
        super().__init__(code)
        self.ambiguous = ambiguous


class NativeDesktopQueue:
    """Linux/macOS adapter for a bundled CLI with native queue capabilities."""

    def __init__(self, codex_binary=None, codex_home=None):
        self.binary = Path(codex_binary) if codex_binary is not None else default_codex_binary()
        self.home = Path(codex_home if codex_home is not None else
                         os.environ.get('CODEX_HOME', Path.home() / '.codex')).expanduser()
        self.desktop = self.binary.parent.parent / 'ChatGPT'
        self.opener = Path('/usr/bin/open' if sys.platform == 'darwin' else '/usr/bin/xdg-open')
        self.bundle = None
        self.cli_aliases = {self.binary.resolve()}
        if sys.platform == 'darwin':
            self._macos_layout()
        self.env = {k: v for k, v in os.environ.items()
                    if not k.startswith(('CODEX_', 'AOT_'))}
        self.env['CODEX_HOME'] = str(self.home)

    def _macos_layout(self):
        # The CLI can be nested inside CodexCLI.app. Match the *outer* app,
        # not the helper bundle, and never accept a PATH CLI as its backend.
        for bundle in self.binary.parents:
            if bundle.suffix != '.app':
                continue
            contents = bundle / 'Contents'
            resources = contents / 'Resources'
            allowed = (resources / 'codex',
                       resources / 'codex-cli/CodexCLI.app/Contents/MacOS/codex')
            if self.binary not in allowed:
                continue
            try:
                path = contents / 'Info.plist'
                info = path.stat()
                if (not stat.S_ISREG(info.st_mode) or info.st_uid not in (0, os.getuid())
                        or info.st_mode & 0o022 or info.st_size > 1024 * 1024):
                    raise ValueError
                metadata = plistlib.loads(path.read_bytes())
                if not isinstance(metadata, dict):
                    raise ValueError
                executable = metadata.get('CFBundleExecutable')
                if (metadata.get('CFBundleIdentifier') != 'com.openai.codex'
                        or not isinstance(executable, str)
                        or re.fullmatch(r'[A-Za-z0-9 _-]+', executable) is None):
                    raise ValueError
                self.desktop = contents / 'MacOS' / executable
                self.bundle = bundle
                self.cli_aliases.add((resources / 'codex-cli/bin/codex').resolve())
                return
            except (OSError, ValueError, plistlib.InvalidFileException):
                break

    def check_capability(self):
        if sys.platform not in ('linux', 'darwin') or (
                sys.platform == 'linux' and not Path('/proc/self/stat').is_file()):
            raise DesktopQueueError('desktop_queue_unsupported_platform')
        try:
            if (not self.binary.is_absolute() or self.binary.name != 'codex'
                    or (sys.platform == 'linux' and self.binary.parent.name != 'resources')
                    or (sys.platform == 'darwin' and self.bundle is None) or not self.home.is_absolute()
                    or not self.home.is_dir()):
                raise ValueError
            self.home = self.home.resolve(strict=True)
            self.env['CODEX_HOME'] = str(self.home)
            for executable in (self.binary, self.desktop, self.opener):
                info = executable.stat()
                if (not stat.S_ISREG(info.st_mode) or info.st_uid not in (0, os.getuid())
                        or info.st_mode & 0o022 or not os.access(executable, os.X_OK)):
                    raise ValueError
            result = subprocess.run([str(self.binary), 'queue', '--help'], stdin=subprocess.DEVNULL,
                                    capture_output=True, text=True, timeout=10, check=False, env=self.env)
            help_text = result.stdout + '\n' + result.stderr
            if result.returncode != 0 or any(
                    re.search(r'(?<![\w-])' + option + r'(?![\w-])', help_text) is None
                    for option in ('--thread', '--message')):
                raise DesktopQueueError('desktop_queue_unsupported_capability')
            # Check only the local-state fields this adapter needs; do not scan history.
            with sqlite3.connect((self.home / 'state_5.sqlite').as_uri() + '?mode=ro', uri=True) as db:
                db.execute('SELECT id,cwd,archived FROM threads WHERE id=?', ('',)).fetchone()
        except DesktopQueueError:
            raise
        except (OSError, ValueError, sqlite3.Error, subprocess.SubprocessError):
            raise DesktopQueueError('desktop_queue_unavailable') from None

    def _routing_matches(self, raw):
        # Only selected routing fields are retained; no authentication is read
        # from disk, logged, copied, or used by this adapter.
        routing = {}
        for pair in raw.split(b'\0'):
            key, _, value = pair.partition(b'=')
            if key in (b'HOME', b'CODEX_HOME', b'CODEX_CLI_PATH', b'CODEX_APP_SERVER_WS_URL',
                       b'CODEX_APP_SERVER_USE_LOCAL_DAEMON'):
                routing[key] = value.decode(errors='strict')
        actual_home = Path(routing.get(b'CODEX_HOME') or
                           str(Path(routing.get(b'HOME', '')) / '.codex'))
        cli = routing.get(b'CODEX_CLI_PATH')
        return (actual_home.is_absolute() and actual_home.resolve() == self.home
                and (not cli or (Path(cli).is_absolute() and Path(cli).resolve() in self.cli_aliases))
                and not routing.get(b'CODEX_APP_SERVER_WS_URL')
                and not routing.get(b'CODEX_APP_SERVER_USE_LOCAL_DAEMON'))

    def _desktop_identity(self):
        """Match the desktop and its direct native local stdio backend.

        Electron erases its main process environment and rewrites helper argv.
        Its direct native app-server child retains the effective Codex home and
        distinguishes this route from a wrapped or external backend.
        """
        if sys.platform == 'darwin':
            return self._macos_desktop_identity()
        mains, backends = {}, []
        for entry in PROC_ROOT.iterdir():
            if not entry.name.isdigit():
                continue
            try:
                if entry.stat().st_uid != os.getuid():
                    continue
                executable = (entry / 'exe').resolve()
                if executable not in (self.desktop.resolve(), self.binary.resolve()):
                    continue
                args = (entry / 'cmdline').read_bytes().split(b'\0')
                fields = (entry / 'stat').read_text().rsplit(')', 1)[1].split()
                pid, parent, start = int(entry.name), int(fields[1]), fields[19]
                if executable == self.desktop.resolve():
                    # Some Electron helpers expose their entire command line
                    # in argv[0], so searching only argument prefixes is wrong.
                    if not any(re.search(rb'(?:^|\s)--type=', arg) for arg in args):
                        mains[pid] = start
                elif (b'app-server' in args and not any(
                        arg.startswith((b'--listen', b'--remote')) for arg in args)
                        and self._routing_matches((entry / 'environ').read_bytes())):
                    backends.append((pid, parent, start))
            except (OSError, ValueError, UnicodeError, IndexError, RuntimeError):
                continue
        identities = [(parent, mains[parent], pid, start) for pid, parent, start in backends
                      if parent in mains]
        return identities[0] if len(identities) == 1 else None

    def _macos_desktop_identity(self):
        from .macos_process import MacOSProcesses
        if self.bundle is None:
            return None
        mains, backends = {}, []
        try:
            for pid, parent, start, executable, args, routing in MacOSProcesses().candidates(
                    {self.desktop.resolve(), self.binary.resolve()}):
                if executable == self.desktop.resolve():
                    if not any(re.search(rb'(?:^|\s)--type=', arg) for arg in args):
                        mains[pid] = start
                elif (b'app-server' in args and not any(
                        arg.startswith((b'--listen', b'--remote')) for arg in args)
                        and self._routing_matches(routing)):
                    backends.append((pid, parent, start))
        except (OSError, ValueError, UnicodeError, RuntimeError):
            return None
        identities = [(parent, mains[parent], pid, start) for pid, parent, start in backends
                      if parent in mains]
        return identities[0] if len(identities) == 1 else None

    def desktop_running(self):
        return self._desktop_identity() is not None

    def validate_target(self, binding):
        if not _uuid(binding.get('thread_id')):
            raise DesktopQueueError('desktop_queue_target_mismatch')
        try:
            with sqlite3.connect((self.home / 'state_5.sqlite').as_uri() + '?mode=ro', uri=True) as db:
                row = db.execute('SELECT id,cwd,archived FROM threads WHERE id=?',
                                 (binding['thread_id'],)).fetchone()
            project = Path(binding['project_dir'])
            if (row is None or row[0] != binding['thread_id'] or row[2] != 0
                    or not isinstance(row[1], str) or not Path(row[1]).is_absolute()
                    or not project.is_absolute() or not project.is_dir()
                    or str(project.resolve(strict=True)) != binding['project_dir']
                    or Path(row[1]).resolve(strict=True) != project):
                raise ValueError
        except (OSError, ValueError, KeyError, sqlite3.Error, RuntimeError, TypeError):
            raise DesktopQueueError('desktop_queue_target_mismatch') from None

    def _guard(self, binding):
        identity = self._desktop_identity()
        if identity is None:
            raise DesktopQueueError('desktop_not_running')
        self.validate_target(binding)
        if self._desktop_identity() != identity:
            raise DesktopQueueError('desktop_process_changed')

    def enqueue(self, binding):
        self._guard(binding)
        try:
            child = subprocess.Popen([str(self.binary), 'queue', '--thread', binding['thread_id'],
                                      '--message', WAKE_NOTICE], cwd=binding['project_dir'], env=self.env,
                                     stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                                     stderr=subprocess.DEVNULL, text=True)
        except OSError as error:
            # These constructor failures mean exec never reached the bundled
            # CLI. Unexpected setup errors remain held rather than guessed safe.
            no_exec = (isinstance(error, (FileNotFoundError, PermissionError))
                       or error.errno in (errno.EMFILE, errno.ENFILE, errno.EAGAIN, errno.ENOMEM))
            raise DesktopQueueError('desktop_queue_start_failed' if no_exec else
                                    'desktop_queue_outcome_unknown', ambiguous=not no_exec) from None
        try:
            stdout, _ = child.communicate(timeout=30)
        except (OSError, subprocess.SubprocessError, UnicodeError):
            # A child existed: it may have committed the queue item before any
            # output/error. Terminate only this owned child, then hold the item.
            try:
                child.kill()
            except OSError:
                pass
            try:
                child.wait(timeout=1)
            except (OSError, subprocess.SubprocessError):
                pass
            raise DesktopQueueError('desktop_queue_outcome_unknown', ambiguous=True) from None
        finally:
            # Avoid leaving captured output descriptors alive after exceptions.
            if child.stdout is not None:
                child.stdout.close()
        match = re.fullmatch(r'Queued message ([0-9a-f-]{36}) for thread '
                             + re.escape(binding['thread_id']) + r'\.\s*', stdout)
        if child.returncode != 0 or match is None or not _uuid(match.group(1)):
            raise DesktopQueueError('desktop_queue_outcome_unknown', ambiguous=True)
        return match.group(1)

    def notice_outcome(self, thread_id, since):
        """Look for one wake notice in the desktop's own records. Read-only.

        Returns ('queued', queue_id) while the notice waits in the native queue,
        ('delivered', None) once the chat holds it as a user message, or
        ('absent', None) when neither database has it since ``since``. Raises
        DesktopQueueError when either database cannot be read, so an unreadable
        record never counts as absent.

        ``since`` is the creation time of the notification being settled. Its
        Telegram message was already collected then, so any turn that a notice
        from after that time started will see it. A notice from an earlier
        notification may have polled before the message arrived, so it must not
        match. Both clocks are this machine's, so no slack is added.

        The two databases are read one after the other. If the desktop moves the
        item from the queue to the chat between the reads, both miss it and the
        wake is enqueued again: the agent gets a second, identical notice, which
        is the accepted cost. The check assumes only ``relay-desktop`` writes
        WAKE_NOTICE into a desktop chat.
        """
        if not _uuid(thread_id):
            raise DesktopQueueError('desktop_queue_target_mismatch')
        since_ms = math.floor(since * 1000)
        try:
            with sqlite3.connect((self.home / 'queue_1.sqlite').as_uri() + '?mode=ro', uri=True) as db:
                row = db.execute('SELECT id FROM queued_items WHERE thread_id=? AND created_at_ms>=? '
                                 'AND instr(payload_json,?)>0 ORDER BY created_at_ms LIMIT 1',
                                 (thread_id, since_ms, WAKE_NOTICE)).fetchone()
            if row is not None:
                if not _uuid(row[0]):
                    raise ValueError
                return 'queued', row[0]
            with sqlite3.connect((self.home / 'thread_history_1.sqlite').as_uri() + '?mode=ro', uri=True) as db:
                row = db.execute("SELECT 1 FROM thread_items WHERE thread_id=? AND created_at_ms>=? "
                                 "AND item_type='userMessage' AND instr(item_json,?)>0 LIMIT 1",
                                 (thread_id, since_ms, WAKE_NOTICE)).fetchone()
        except (OSError, sqlite3.Error, ValueError):
            raise DesktopQueueError('desktop_records_unreadable') from None
        return ('delivered', None) if row is not None else ('absent', None)

    def thread_busy(self, thread_id, now):
        """True while the chat's latest turn is in progress and recently active. Read-only.

        Unreadable records count as not busy, so the listener falls back to
        queueing, which was the behavior before this check.
        """
        if not _uuid(thread_id):
            return False
        try:
            with sqlite3.connect((self.home / 'thread_history_1.sqlite').as_uri() + '?mode=ro', uri=True) as db:
                row = db.execute(
                    'SELECT t.status, t.started_at, (SELECT MAX(i.created_at_ms) FROM thread_items i '
                    'WHERE i.thread_id=t.thread_id AND i.turn_id=t.turn_id) '
                    'FROM thread_turns t WHERE t.thread_id=? ORDER BY t.rollout_ordinal DESC LIMIT 1',
                    (thread_id,)).fetchone()
        except (OSError, sqlite3.Error, ValueError):
            return False
        # thread_turns.started_at is in seconds and thread_items.created_at_ms in
        # milliseconds (checked against a live desktop database).
        if row is None or row[0] != 'inProgress':
            return False
        activity = [value for value in (row[1], row[2] / 1000 if isinstance(row[2], (int, float)) else None)
                    if isinstance(value, (int, float))]
        return bool(activity) and now - max(activity) <= BUSY_TURN_ACTIVITY_SECONDS

    def wake(self, binding):
        self._guard(binding)
        try:
            command = [str(self.opener)]
            if self.bundle is not None:
                # Pin the selected installed app rather than the default URL handler.
                command += ['-a', str(self.bundle)]
            result = subprocess.run([*command, 'codex://threads/wake/' + binding['thread_id']],
                                    stdin=subprocess.DEVNULL, capture_output=True, timeout=20, check=False,
                                    env=self.env)
            if result.returncode != 0:
                raise DesktopQueueError('desktop_wake_failed')
        except (OSError, subprocess.SubprocessError):
            raise DesktopQueueError('desktop_wake_failed') from None


class DesktopHandoffs:
    """Queue receipts survive restarts independently of inbox ACKs and wake URLs."""

    def __init__(self, store):
        self.store = store
        self.core = store.core

    def prepare(self, event):
        with self.core._tx() as db:
            db.execute('''INSERT INTO relay_desktop_handoffs
                (notification_id,phase,queue_id,wake_attempt,next_wake_at,updated)
                VALUES(?,'enqueuing',NULL,0,NULL,?)''', (event['notification_id'], self.core.clock()))

    def queued(self, event, queue_id):
        if not _uuid(queue_id):
            raise DesktopQueueError('desktop_queue_outcome_unknown', ambiguous=True)
        with self.core._tx() as db:
            now = self.core.clock()
            db.execute('''UPDATE relay_desktop_handoffs SET phase='queued',queue_id=?,
                next_wake_at=?,updated=? WHERE notification_id=? AND phase='enqueuing' ''',
                       (queue_id, now, now, event['notification_id']))
            # The native queue accepted work, but URL handoff has not completed.
            # This state blocks every other relay mode from enqueuing a duplicate.
            db.execute("UPDATE relay_notifications SET status='uncertain',updated=?,next_attempt_at=NULL WHERE id=?",
                       (now, event['notification_id']))

    def unknown_enqueues(self, older_than):
        """Handoffs whose enqueue outcome was unknown and that still have no receipt."""
        with self.core._db() as db:
            return [dict(row) for row in db.execute('''SELECT n.id AS notification_id,
                n.client_id,n.thread_id,n.project_dir,n.created AS since
                FROM relay_desktop_handoffs h JOIN relay_notifications n ON n.id=h.notification_id
                WHERE h.phase='enqueuing' AND h.queue_id IS NULL AND n.status='uncertain'
                AND h.updated<=? ORDER BY n.created,n.id''', (older_than,))]

    def delivered(self, event):
        """The desktop chat already holds the notice: record it as handed off."""
        receipt = self.store.resolve(event['notification_id'], 'accepted')
        with self.core._tx() as db:
            db.execute("UPDATE relay_desktop_handoffs SET phase='done',next_wake_at=NULL,updated=? WHERE notification_id=?",
                       (self.core.clock(), event['notification_id']))
        return receipt

    def pending(self):
        with self.core._db() as db:
            return [dict(row) for row in db.execute('''SELECT n.id AS notification_id,
                n.client_id,n.thread_id,n.project_dir,h.queue_id,h.wake_attempt
                FROM relay_desktop_handoffs h JOIN relay_notifications n ON n.id=h.notification_id
                WHERE h.phase='queued' AND n.status IN ('inflight','uncertain')
                AND h.next_wake_at<=? ORDER BY n.created,n.id''', (self.core.clock(),))]

    def wake_started(self, event):
        # Persist a backoff before invoking the opener, including process crashes.
        with self.core._tx() as db:
            row = db.execute('SELECT wake_attempt FROM relay_desktop_handoffs WHERE notification_id=?',
                             (event['notification_id'],)).fetchone()
            attempt = row['wake_attempt'] + 1
            now = self.core.clock()
            db.execute('''UPDATE relay_desktop_handoffs SET wake_attempt=?,next_wake_at=?,updated=?
                WHERE notification_id=?''', (attempt, now + min(60, 2 ** min(attempt - 1, 6)),
                                             now, event['notification_id']))

    def done(self, event):
        receipt = self.store.resolve(event['notification_id'], 'accepted')
        with self.core._tx() as db:
            db.execute("UPDATE relay_desktop_handoffs SET phase='done',next_wake_at=NULL,updated=? WHERE notification_id=?",
                       (self.core.clock(), event['notification_id']))
        return receipt


def run_desktop_dispatcher(core, store, *, codex_binary=None, codex_home=None,
                           duration=55, collect=False, interval=.5, emit=None, stop_event=None):
    """Run with the ordinary desktop open; duration 0 is explicitly continuous.

    Acceptance means the native queue accepted the note and the OS accepted the
    wake URL, not that a turn completed or any Telegram message was handled.
    """
    if (type(collect) is not bool or isinstance(duration, bool)
            or not isinstance(duration, (int, float)) or not math.isfinite(duration)
            or not 0 <= duration <= 86400 or isinstance(interval, bool)
            or not isinstance(interval, (int, float)) or not math.isfinite(interval)
            or not .01 <= interval <= 60):
        raise CoreError('invalid_desktop_dispatcher_options')
    if emit is None:
        def emit(value):
            print(json.dumps(value, separators=(',', ':'), ensure_ascii=True), flush=True)
    transport = NativeDesktopQueue(codex_binary=codex_binary, codex_home=codex_home)
    stop = threading.Event() if stop_event is None else stop_event
    deadline = time.monotonic() + duration if duration else float('inf')
    collector = None
    handlers = {}
    errors = queue.Queue(maxsize=1)
    counters = {'accepted': 0, 'failed': 0, 'uncertain': 0, 'wake_retries': 0}
    reason = 'duration'
    handoffs = DesktopHandoffs(store)
    last_errors = {}
    route_backoff = {}
    stuck_warned = {}
    stuck_check_at = 0.0

    def settle_unknown_enqueues():
        # An enqueue whose child died without a receipt may or may not have
        # queued the notice. The desktop's own records answer that: still queued
        # (keep its id and wake it), already in the chat (accepted), or in
        # neither (it never landed, so retry). Unreadable records settle nothing.
        older_than = core.clock() - UNKNOWN_ENQUEUE_CHECK_AFTER_SECONDS
        for event in handoffs.unknown_enqueues(older_than):
            try:
                found, queue_id = transport.notice_outcome(event['thread_id'], event['since'])
                if found == 'queued':
                    handoffs.queued(event, queue_id)
                    outcome, receipt = 'queued', {'queue_id': queue_id}
                elif found == 'delivered':
                    outcome, receipt = 'accepted', handoffs.delivered(event)
                else:
                    outcome, receipt = 'failed', store.resolve(event['notification_id'], 'failed')
            except CoreError as error:
                report(event['notification_id'], {'type': 'desktop_unknown_enqueue_unsettled',
                       'notification_id': event['notification_id'], 'error': _code(error)})
                continue
            emit({'type': 'desktop_unknown_enqueue_settled', 'client_id': event['client_id'],
                  'thread_id': event['thread_id'], 'outcome': outcome,
                  'notification_id': event['notification_id'], **receipt})

    def warn_stuck_wakes():
        # Report only. An unresolved handoff may already have reached the target,
        # so DIRECT_DISPATCH.md requires a human to reconcile it.
        now = time.monotonic()
        wakes = store.unresolved_wakes()
        current = {wake['notification_id'] for wake in wakes}
        for key in set(stuck_warned) - current:
            del stuck_warned[key]
        for wake in wakes:
            if now < stuck_warned.get(wake['notification_id'], 0):
                continue
            stuck_warned[wake['notification_id']] = now + STUCK_WAKE_WARNING_REPEAT_SECONDS
            emit({'type': 'desktop_wake_stuck', 'level': 'warning', **wake,
                  'message': STUCK_WAKE_MESSAGES[wake['next_action']]})

    def report(key, event):
        if last_errors.get(key) != event.get('error'):
            emit(event)
            last_errors[key] = event.get('error')

    def signal_stop(_signum, _frame):
        stop.set()

    def collect_loop():
        backoff = interval
        while not stop.is_set():
            try:
                core.collect(timeout=10, judge=True)
                backoff = interval
            except Exception as error:
                try:
                    errors.put_nowait(_code(error))
                except queue.Full:
                    pass
                backoff = min(60, max(interval, backoff * 2))
            stop.wait(backoff)

    with core._lock('relay-watcher') as acquired:
        if not acquired:
            raise CoreError('relay_watcher_busy')
        try:
            transport.check_capability()
            if threading.current_thread() is threading.main_thread():
                for signum in (signal.SIGTERM, signal.SIGINT):
                    handlers[signum] = signal.signal(signum, signal_stop)
            recovered = store.recover_inflight()
            emit({'type': 'desktop_dispatcher_ready', 'duration': duration, 'collect': collect,
                  'recovered': recovered, 'delivery_role': 'user'})
            if collect:
                collector = threading.Thread(target=collect_loop, name='aot-desktop-collector', daemon=True)
                collector.start()
            while not stop.is_set() and time.monotonic() < deadline:
                try:
                    error = errors.get_nowait()
                except queue.Empty:
                    pass
                else:
                    report('collector', {'type': 'desktop_collection_error', 'error': error})
                if time.monotonic() >= stuck_check_at:
                    stuck_check_at = time.monotonic() + STUCK_WAKE_CHECK_SECONDS
                    settle_unknown_enqueues()
                    warn_stuck_wakes()
                if not transport.desktop_running():
                    report('desktop', {'type': 'desktop_waiting', 'error': 'desktop_not_running'})
                    stop.wait(min(interval, max(0, deadline - time.monotonic())))
                    continue
                last_errors.pop('desktop', None)
                bindings = {b['client_id']: b for b in store.status()['bindings']}
                # Accepted native queue items are never re-enqueued, even after
                # a failed opener or process restart. Retry only the wake URL.
                for event in handoffs.pending():
                    binding = bindings.get(event['client_id'])
                    if (stop.is_set() or binding is None or not binding['eligible']
                            or (binding['thread_id'], binding['project_dir']) !=
                               (event['thread_id'], event['project_dir'])):
                        continue
                    try:
                        handoffs.wake_started(event)
                        if stop.is_set():
                            break
                        transport.wake(event)
                    except DesktopQueueError as error:
                        counters['wake_retries'] += 1
                        report(event['notification_id'], {'type': 'desktop_wake_pending',
                               'notification_id': event['notification_id'], 'queue_id': event['queue_id'],
                               'error': _code(error)})
                    else:
                        receipt = handoffs.done(event)
                        counters['accepted'] += 1
                        emit({'type': 'desktop_dispatcher_receipt', 'client_id': event['client_id'],
                              'thread_id': event['thread_id'], 'queue_id': event['queue_id'],
                              'outcome': 'accepted', **receipt})
                allowed = {}
                for binding in bindings.values():
                    client = binding['client_id']
                    if (not binding['eligible'] or not binding['dispatchable_messages']
                            or time.monotonic() < route_backoff.get(client, 0)):
                        continue
                    try:
                        transport.validate_target(binding)
                    except DesktopQueueError as error:
                        route_backoff[client] = time.monotonic() + 5
                        report(client, {'type': 'desktop_route_blocked', 'client_id': client,
                                        'thread_id': binding['thread_id'], 'error': _code(error)})
                    else:
                        last_errors.pop(client, None)
                        allowed[client] = (binding['thread_id'], binding['project_dir'])
                if not stop.is_set() and transport.desktop_running():
                    event = store.next_event(allowed_client_ids=set(allowed))
                    if event is not None:
                        current = next((b for b in store.status(event['client_id'])['bindings']
                                        if b['client_id'] == event['client_id']), None)
                        if (stop.is_set() or not transport.desktop_running()
                                or current is None or not current['eligible']
                                or allowed.get(event['client_id']) != (event['thread_id'], event['project_dir'])
                                or (current['thread_id'], current['project_dir']) !=
                                   (event['thread_id'], event['project_dir'])):
                            store.resolve(event['notification_id'], 'failed')
                        elif transport.thread_busy(event['thread_id'], core.clock()):
                            # The hooks reach a busy chat inside its turn. Try
                            # again later; once it is idle, the notice is queued.
                            receipt = store.resolve(event['notification_id'], 'failed')
                            report('busy:' + event['client_id'], {'type': 'desktop_wake_deferred_busy',
                                   'client_id': event['client_id'], 'thread_id': event['thread_id'],
                                   'error': 'thread_busy', **receipt})
                        else:
                            last_errors.pop('busy:' + event['client_id'], None)
                            handoffs.prepare(event)
                            if stop.is_set() or not transport.desktop_running():
                                store.resolve(event['notification_id'], 'failed')
                                continue
                            try:
                                queue_id = transport.enqueue(event)
                                handoffs.queued(event, queue_id)
                            except DesktopQueueError as error:
                                outcome = 'uncertain' if error.ambiguous else 'failed'
                                receipt = store.resolve(event['notification_id'], outcome)
                                counters[outcome] += 1
                                emit({'type': 'desktop_dispatcher_receipt', 'client_id': event['client_id'],
                                      'thread_id': event['thread_id'], 'outcome': outcome,
                                      'error': _code(error), **receipt})
                            else:
                                emit({'type': 'desktop_queued', 'notification_id': event['notification_id'],
                                      'thread_id': event['thread_id'], 'queue_id': queue_id})
                stop.wait(min(interval, max(0, deadline - time.monotonic())))
            if stop.is_set():
                reason = 'stopped'
        except KeyboardInterrupt:
            reason = 'stopped'
        finally:
            stop.set()
            if collector is not None:
                collector.join(timeout=.2)
            store.recover_inflight()
            for signum, handler in handlers.items():
                signal.signal(signum, handler)
        result = {'type': 'desktop_dispatcher_stopped', 'reason': reason, **counters}
        emit(result)
        return result
