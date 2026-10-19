"""Native queue subprocess boundary, with disposable local metadata only."""
import errno
import io
import json
import os
from pathlib import Path
import sqlite3
import stat
import subprocess
from types import SimpleNamespace

import pytest

from agents_over_telegram import desktop_dispatcher as desktop
from agents_over_telegram.relay import WAKE_NOTICE
from test_relay import ALPHA_THREAD, BETA_THREAD


QUEUE_ID = '01a154d9-6676-7dd1-b4b2-d1accff1d4df'


@pytest.fixture
def adapter(tmp_path, monkeypatch):
    install = tmp_path / 'application'
    binary = install / 'resources' / 'codex'
    binary.parent.mkdir(parents=True)
    for file in (binary, install / 'ChatGPT'):
        file.write_text('#!/bin/sh\nexit 99\n')
        file.chmod(0o700)
    home = tmp_path / 'codex-home'
    home.mkdir()
    project = tmp_path / 'project'
    project.mkdir()
    with sqlite3.connect(home / 'state_5.sqlite') as db:
        db.execute('CREATE TABLE threads(id TEXT PRIMARY KEY, cwd TEXT, archived INTEGER)')
        db.execute('INSERT INTO threads VALUES(?,?,0)', (ALPHA_THREAD, str(project)))
    binding = {'thread_id': ALPHA_THREAD, 'project_dir': str(project),
               'notice': 'INJECTED BINDING NOTICE MUST NOT BECOME PROMPT'}
    queue = desktop.NativeDesktopQueue(binary, home)
    monkeypatch.setattr(queue, '_desktop_identity', lambda: (12345, '67890'))
    state = {'calls': [], 'exit': 0, 'version': desktop.SUPPORTED_CODEX_VERSION,
             'stdout': f'Queued message {QUEUE_ID} for thread {ALPHA_THREAD}.\n', 'error': None,
             'spawn_error': None, 'communicate_error': None, 'processes': [],
             'communicate_timeouts': [], 'wait_timeouts': [], 'kills': 0}
    def subprocess_run(command, **kwargs):
        state['calls'].append((command, kwargs))
        if state['error']:
            raise state['error']
        stdout = (state['version'] + '\n'
                  if command[-1] == '--version' else state['stdout'])
        return subprocess.CompletedProcess(command, state['exit'], stdout, 'SECRET STDERR')
    monkeypatch.setattr(desktop.subprocess, 'run', subprocess_run)

    class Process:
        def __init__(self, command, **kwargs):
            state['calls'].append((command, kwargs))
            if state['spawn_error']:
                raise state['spawn_error']
            state['processes'].append(self)
            self.returncode = None
            self.stdout = io.StringIO()
            self.stderr = io.StringIO()

        def communicate(self, timeout=None):
            state['communicate_timeouts'].append(timeout)
            if state['communicate_error'] and self.returncode is None:
                raise state['communicate_error']
            self.returncode = state['exit'] if self.returncode is None else self.returncode
            return state['stdout'], 'SECRET STDERR'

        def poll(self):
            return self.returncode

        def kill(self):
            state['kills'] += 1
            self.returncode = -9

        def wait(self, timeout=None):
            state['wait_timeouts'].append(timeout)
            self.returncode = state['exit'] if self.returncode is None else self.returncode
            return self.returncode

    monkeypatch.setattr(desktop.subprocess, 'Popen', Process)
    return queue, binding, state


def test_enqueue_uses_only_fixed_notice_and_validated_uuid(adapter):
    queue, binding, state = adapter
    assert queue.enqueue(binding) == QUEUE_ID
    command, options = state['calls'][0]
    assert command == [str(queue.binary), 'queue', '--thread', ALPHA_THREAD, '--message', WAKE_NOTICE]
    assert options['cwd'] == binding['project_dir']
    assert options['stdin'] == subprocess.DEVNULL
    assert options['stdout'] == subprocess.PIPE and options['stderr'] == subprocess.DEVNULL
    assert state['communicate_timeouts'] == [30]
    assert options['env']['CODEX_HOME'] == str(queue.home)
    assert 'INJECTED BINDING' not in json.dumps(command)


@pytest.mark.parametrize('stdout', [
    '', 'not a queue receipt\n',
    f'Queued message {QUEUE_ID} for thread {BETA_THREAD}.\n',
    f'Queued message {"-" * 36} for thread {ALPHA_THREAD}.\n',
    f'Queued message {QUEUE_ID} for thread {ALPHA_THREAD}.\nUNEXPECTED EXTRA OUTPUT',
])
def test_invalid_or_mismatched_stdout_is_ambiguous(adapter, stdout):
    queue, binding, state = adapter
    state['stdout'] = stdout
    with pytest.raises(desktop.DesktopQueueError) as error:
        queue.enqueue(binding)
    assert error.value.ambiguous is True
    assert 'SECRET STDERR' not in str(error.value)
    assert len(state['calls']) == 1


def test_nonzero_even_with_success_receipt_is_ambiguous(adapter):
    queue, binding, state = adapter
    state['exit'] = 1
    with pytest.raises(desktop.DesktopQueueError) as error:
        queue.enqueue(binding)
    assert error.value.ambiguous is True


@pytest.mark.parametrize('failure', ['timeout', 'subprocess', 'decode', 'oserror'])
def test_inflight_process_failures_are_ambiguous(adapter, failure):
    queue, binding, state = adapter
    state['communicate_error'] = {'timeout': subprocess.TimeoutExpired(['codex', 'queue'], 30),
                                  'subprocess': subprocess.SubprocessError('sensitive output'),
                                  'decode': UnicodeDecodeError('utf8', b'\xff', 0, 1, 'bad byte'),
                                  'oserror': OSError(errno.EIO, 'sensitive output')}[failure]
    with pytest.raises(desktop.DesktopQueueError) as error:
        queue.enqueue(binding)
    assert error.value.ambiguous is True
    assert 'sensitive output' not in str(error.value)
    assert len(state['processes']) == 1 and state['kills'] == 1
    assert state['communicate_timeouts'][0] == 30
    assert state['wait_timeouts'] and all(0 < value <= 5 for value in state['wait_timeouts'])
    assert state['processes'][0].stdout.closed


@pytest.mark.parametrize('failure', [
    FileNotFoundError(errno.ENOENT, 'secret path'),
    PermissionError(errno.EACCES, 'secret path'),
    *[OSError(code, 'secret path') for code in (errno.EMFILE, errno.ENFILE, errno.EAGAIN, errno.ENOMEM)],
])
def test_known_pre_spawn_failure_is_definite_no_enqueue(adapter, failure):
    queue, binding, state = adapter
    state['spawn_error'] = failure
    with pytest.raises(desktop.DesktopQueueError) as error:
        queue.enqueue(binding)
    assert error.value.ambiguous is False
    assert 'secret path' not in str(error.value)
    assert state['processes'] == []
    assert state['communicate_timeouts'] == state['wait_timeouts'] == []
    assert state['kills'] == 0


def test_unclassified_constructor_oserror_is_conservatively_ambiguous(adapter):
    queue, binding, state = adapter
    state['spawn_error'] = OSError(errno.EIO, 'secret path')
    with pytest.raises(desktop.DesktopQueueError) as error:
        queue.enqueue(binding)
    assert error.value.ambiguous is True
    assert error.value.code == 'desktop_queue_outcome_unknown'
    assert 'secret path' not in str(error.value)
    assert state['processes'] == [] and state['kills'] == 0


@pytest.mark.parametrize('change', ['invalid_uuid', 'unknown_uuid', 'wrong_cwd', 'archived', 'relative_cwd'])
def test_target_validation_rejects_without_subprocess(adapter, change):
    queue, binding, state = adapter
    if change == 'invalid_uuid':
        binding['thread_id'] = 'not-a-uuid?prompt=bad'
    elif change == 'unknown_uuid':
        binding['thread_id'] = BETA_THREAD
    else:
        with sqlite3.connect(queue.home / 'state_5.sqlite') as db:
            if change == 'archived':
                db.execute('UPDATE threads SET archived=1')
            else:
                db.execute('UPDATE threads SET cwd=?', ('/tmp' if change == 'wrong_cwd' else 'project',))
    with pytest.raises(desktop.DesktopQueueError, match='desktop_queue_target_mismatch'):
        queue.enqueue(binding)
    assert state['calls'] == []


@pytest.mark.parametrize('operation', ['enqueue', 'wake'])
def test_closed_desktop_never_calls_cli_or_url_opener(adapter, monkeypatch, operation):
    queue, binding, state = adapter
    monkeypatch.setattr(queue, '_desktop_identity', lambda: None)
    with pytest.raises(desktop.DesktopQueueError, match='desktop_not_running'):
        getattr(queue, operation)(binding)
    assert state['calls'] == []


@pytest.mark.parametrize('operation', ['enqueue', 'wake'])
def test_desktop_changes_during_target_validation_never_submits(adapter, monkeypatch, operation):
    queue, binding, state = adapter
    identities = iter([(12345, 'first'), (12345, 'reused-pid')])
    monkeypatch.setattr(queue, '_desktop_identity', lambda: next(identities))
    with pytest.raises(desktop.DesktopQueueError, match='desktop_process_changed'):
        getattr(queue, operation)(binding)
    assert state['calls'] == []


def test_wake_url_contains_only_uuid_and_no_prompt(adapter):
    queue, binding, state = adapter
    queue.wake(binding)
    command, options = state['calls'][0]
    assert command == ['/usr/bin/xdg-open', 'codex://threads/wake/' + ALPHA_THREAD]
    assert options['timeout'] == 20
    assert 'INJECTED BINDING' not in json.dumps(command)


def test_capability_gate_rejects_untested_version(adapter, monkeypatch):
    queue, _, state = adapter
    # Container UID mapping can expose host root as nobody; this unit fixture
    # supplies its own trusted-opener metadata instead of requiring host layout.
    path_stat = Path.stat
    file_access = os.access
    monkeypatch.setattr(Path, 'stat', lambda path, **kwargs:
        SimpleNamespace(st_mode=stat.S_IFREG | 0o755, st_uid=0)
        if path == Path('/usr/bin/xdg-open') else path_stat(path, **kwargs))
    monkeypatch.setattr(os, 'access', lambda path, mode:
        True if path == Path('/usr/bin/xdg-open') else file_access(path, mode))
    queue.check_capability()
    state['version'] = 'codex-cli 999.0.0-untested'
    with pytest.raises(desktop.DesktopQueueError, match='desktop_queue_unsupported_version'):
        queue.check_capability()
    assert all(command[-1] == '--version' for command, _ in state['calls'])


def test_custom_launch_routing_variables_are_not_propagated(tmp_path, monkeypatch):
    for name in ('CODEX_CLI_PATH', 'CODEX_APP_SERVER_WS_URL', 'CODEX_APP_SERVER_USE_LOCAL_DAEMON',
                 'AOT_DISPATCHER_CMD'):
        monkeypatch.setenv(name, 'unwanted-routing-value')
    queue = desktop.NativeDesktopQueue(codex_home=tmp_path)
    assert {key for key in queue.env if key.startswith(('CODEX_', 'AOT_'))} == {'CODEX_HOME'}


@pytest.fixture
def processes(adapter, tmp_path, monkeypatch):
    queue, _, _ = adapter
    root = tmp_path / 'proc-fixture'
    root.mkdir()
    monkeypatch.setattr(desktop, 'PROC_ROOT', root)
    # The adapter fixture bypasses process discovery for subprocess-only tests.
    # Here restore the actual discovery method over the fake proc inventory.
    monkeypatch.setattr(queue, '_desktop_identity',
        desktop.NativeDesktopQueue._desktop_identity.__get__(queue))

    def add(pid, parent, executable, args, environment=None, start='123'):
        entry = root / str(pid)
        entry.mkdir()
        (entry / 'exe').symlink_to(executable)
        (entry / 'cmdline').write_bytes(b'\0'.join(arg.encode() for arg in args) + b'\0')
        fields = ['S', str(parent)] + ['0'] * 17 + [start]
        (entry / 'stat').write_text(f'{pid} (process name with spaces) ' + ' '.join(fields))
        if environment is not None:
            (entry / 'environ').write_bytes(b'\0'.join(
                key.encode() + b'=' + value.encode() for key, value in environment.items()) + b'\0')
        return entry

    return queue, add


def test_normal_desktop_is_found_from_native_child_with_cleared_main_environment(processes):
    queue, add = processes
    add(100, 1, queue.desktop, [str(queue.desktop)])
    add(101, 100, queue.binary, [str(queue.binary), '-c', 'features.code_mode_host=true', 'app-server'],
        {'CODEX_HOME': str(queue.home), 'CODEX_CLI_PATH': str(queue.binary)}, start='456')
    assert queue._desktop_identity() == (100, '123', 101, '456')


@pytest.mark.parametrize('args', [
    ['/application/ChatGPT', '--type=renderer'],
    ['/application/ChatGPT --type=renderer --other-argument'],
])
def test_electron_helpers_with_separate_or_merged_arguments_are_not_main(processes, args):
    queue, add = processes
    add(100, 1, queue.desktop, args)
    add(101, 100, queue.binary, [str(queue.binary), 'app-server'], {'CODEX_HOME': str(queue.home)})
    assert queue.desktop_running() is False


@pytest.mark.parametrize('routing', ['wrapper', 'external_ws', 'daemon', 'wrong_home'])
def test_backend_routing_overrides_are_rejected(processes, routing):
    queue, add = processes
    values = {'CODEX_HOME': str(queue.home)}
    key, value = {'wrapper': ('CODEX_CLI_PATH', '/tmp/custom-codex-wrapper'),
                  'external_ws': ('CODEX_APP_SERVER_WS_URL', 'ws://127.0.0.1:1234'),
                  'daemon': ('CODEX_APP_SERVER_USE_LOCAL_DAEMON', '1'),
                  'wrong_home': ('CODEX_HOME', '/tmp/different-home')}[routing]
    values[key] = value
    add(100, 1, queue.desktop, [str(queue.desktop)])
    add(101, 100, queue.binary, [str(queue.binary), 'app-server'], values)
    assert queue.desktop_running() is False


@pytest.mark.parametrize('mode', ['listen', 'remote', 'indirect', 'duplicate'])
def test_only_one_direct_native_stdio_backend_is_accepted(processes, mode):
    queue, add = processes
    add(100, 1, queue.desktop, [str(queue.desktop)])
    args = [str(queue.binary), 'app-server']
    if mode == 'listen':
        args += ['--listen', 'unix:///tmp/private.sock']
    elif mode == 'remote':
        args += ['--remote-control']
    add(101, 999 if mode == 'indirect' else 100, queue.binary, args, {'CODEX_HOME': str(queue.home)})
    if mode == 'duplicate':
        add(102, 100, queue.binary, args, {'CODEX_HOME': str(queue.home)})
    assert queue.desktop_running() is False


def test_home_fallback_matches_only_actual_codex_home(processes):
    queue, add = processes
    queue.home = queue.home.parent / '.codex'
    add(100, 1, queue.desktop, [str(queue.desktop)])
    add(101, 100, queue.binary, [str(queue.binary), 'app-server'], {'HOME': str(queue.home.parent)})
    assert queue.desktop_running() is True
