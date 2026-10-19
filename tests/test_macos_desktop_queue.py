"""Darwin layout/process boundaries using disposable metadata, on any host."""
import ctypes
import os
from pathlib import Path
import plistlib
import sqlite3
import stat
import struct
import subprocess
import sys
from types import SimpleNamespace

import pytest

from agents_over_telegram import desktop_dispatcher as desktop
from agents_over_telegram import macos_process as macos
from agents_over_telegram import desktop_bridge as bridge
from test_relay import ALPHA_THREAD


def procargs(args, environment=()):
    return (struct.pack('=i', len(args)) + b'/selected/executable\0\0\0'
            + b'\0'.join(args) + b'\0' + b'\0'.join(environment) + b'\0')


def test_procargs_keeps_only_routing_and_handles_empty_arguments():
    args, routing = macos.parse_procargs(procargs(
        [b'/app/codex', b'app-server', b'', b'--analytics-default-enabled'],
        [b'HOME=/home/owner', b'CODEX_HOME=/private/state',
         b'TELEGRAM_BOT_TOKEN=secret-not-retained', b'AWS_SECRET_ACCESS_KEY=also-secret']))
    assert args == [b'/app/codex', b'app-server', b'', b'--analytics-default-enabled']
    assert routing == b'HOME=/home/owner\0CODEX_HOME=/private/state'
    assert b'secret' not in routing


@pytest.mark.parametrize('raw', [b'', struct.pack('=i', -1), struct.pack('=i', 0) + b'\0',
                               struct.pack('=i', 100000) + b'\0',
                               struct.pack('=i', 2) + b'/app\0\0only-one-arg\0'])
def test_malformed_process_arguments_rejected(raw):
    with pytest.raises(ValueError):
        macos.parse_procargs(raw)


def test_duplicate_routing_fields_fail_closed():
    with pytest.raises(ValueError):
        macos.parse_procargs(procargs([b'codex'], [b'CODEX_HOME=/one', b'CODEX_HOME=/two']))


@pytest.mark.parametrize('condition', ['foreign_owner', 'foreign_real_owner', 'changed_pid', 'short_info'])
def test_darwin_bsd_metadata_rejects_wrong_owner_or_incomplete_identity(condition):
    reader = object.__new__(macos.MacOSProcesses)
    def info(pid, flavor, arg, buffer, size):
        assert flavor == 3
        value = ctypes.cast(buffer, ctypes.POINTER(macos._BSDInfo)).contents
        value.pid = pid + (condition == 'changed_pid')
        value.uid = os.getuid() + (condition == 'foreign_owner')
        value.ruid = os.getuid() + (condition == 'foreign_real_owner')
        return size - (condition == 'short_info')
    reader.proc = SimpleNamespace(proc_pidinfo=info)
    with pytest.raises((OSError, ValueError)):
        reader._info(123)


@pytest.mark.parametrize('race', ['start', 'executable', 'none'])
def test_darwin_candidate_rechecks_start_and_path_and_skips_unrelated_environment(monkeypatch, race):
    reader = object.__new__(macos.MacOSProcesses)
    def listpids(kind, arg, buffer, size):
        if buffer is None:
            return 8
        buffer[0], buffer[1] = 100, 101
        return 8
    reader.proc = SimpleNamespace(proc_listpids=listpids)
    counts = {'info': 0, 'path': 0, 'args': []}
    def info(pid):
        counts['info'] += pid == 101
        return pid, 1, (999 if race == 'start' and counts['info'] == 2 else 123, 456)
    def path(pid):
        counts['path'] += pid == 101
        return Path('/unrelated') if pid == 100 or (race == 'executable' and counts['path'] == 2) else Path('/selected')
    def arguments(*mib):
        counts['args'].append(mib)
        return procargs([b'/selected', b'app-server'], [b'HOME=/home/test', b'SECRET=discarded'])
    monkeypatch.setattr(reader, '_info', info)
    monkeypatch.setattr(reader, '_path', path)
    monkeypatch.setattr(reader, '_sysctl', arguments)
    records = list(reader.candidates({Path('/selected')}))
    assert counts['args'] == [(1, 49, 101)]
    assert len(records) == (1 if race == 'none' else 0)
    if records:
        assert records[0][-1] == b'HOME=/home/test'


@pytest.mark.parametrize('count,error,empty', [(0, 0, True), (4, 0, False), (0, 1, False), (-1, 0, False)])
def test_empty_group_requires_successful_libproc_evidence(count, error, empty):
    reader = object.__new__(macos.MacOSProcesses)
    def listpids(kind, pgid, buffer, size):
        assert kind == 2 and pgid == 123
        ctypes.set_errno(error)
        return count
    reader.proc = SimpleNamespace(proc_listpids=listpids)
    assert reader.group_is_empty(123) is empty
    assert reader.group_is_empty(0) is False


@pytest.mark.parametrize('platform,exited,empty,allowed', [
    ('darwin', True, True, True), ('darwin', False, True, False),
    ('darwin', True, False, False), ('linux', True, True, False)])
def test_backend_eperm_is_ignored_only_for_verified_empty_mac_group(monkeypatch, platform, exited, empty, allowed):
    monkeypatch.setattr(bridge.sys, 'platform', platform)
    def denied(*args):
        raise PermissionError('not allowed')
    monkeypatch.setattr(bridge.os, 'killpg', denied)
    monkeypatch.setattr(macos, 'MacOSProcesses', lambda:
        SimpleNamespace(group_is_empty=lambda pid: pid == 123 and empty))
    process = SimpleNamespace(pid=123, poll=lambda: 7 if exited else None, wait=lambda **kw: 7)
    if allowed:
        bridge.stop_backend(process)
    else:
        with pytest.raises(PermissionError):
            bridge.stop_backend(process)


@pytest.fixture
def mac_adapter(tmp_path, monkeypatch):
    monkeypatch.setattr(desktop.sys, 'platform', 'darwin')
    bundle = tmp_path / 'Application With Spaces.app'
    contents = bundle / 'Contents'
    binary = contents / 'Resources/codex-cli/CodexCLI.app/Contents/MacOS/codex'
    main = contents / 'MacOS/ChatGPT'
    for path in (binary, main):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text('#!/bin/sh\nexit 99\n')
        path.chmod(0o700)
    (contents / 'Info.plist').write_bytes(plistlib.dumps(
        {'CFBundleIdentifier': 'com.openai.codex', 'CFBundleExecutable': 'ChatGPT'}))
    home = tmp_path / 'state'
    home.mkdir()
    project = tmp_path / 'project'
    project.mkdir()
    with sqlite3.connect(home / 'state_5.sqlite') as db:
        db.execute('CREATE TABLE threads(id TEXT PRIMARY KEY,cwd TEXT,archived INTEGER)')
        db.execute('INSERT INTO threads VALUES(?,?,0)', (ALPHA_THREAD, str(project)))
    q = desktop.NativeDesktopQueue(binary, home)
    records = [(100, 1, (123, 456), main.resolve(), [str(main).encode()], b''),
               (101, 100, (234, 567), binary.resolve(), [str(binary).encode(), b'app-server'],
                b'CODEX_HOME=' + os.fsencode(home))]
    class Processes:
        def candidates(self, executables):
            assert executables == {main.resolve(), binary.resolve()}
            yield from records
    monkeypatch.setattr(macos, 'MacOSProcesses', Processes)
    return q, {'thread_id': ALPHA_THREAD, 'project_dir': str(project)}, records


def test_mac_direct_backend_and_microsecond_start_identity(mac_adapter):
    q, _, _ = mac_adapter
    assert q._desktop_identity() == (100, (123, 456), 101, (234, 567))


@pytest.mark.parametrize('change', ['missing_main', 'indirect', 'duplicate', 'remote', 'listen',
                                  'wrong_home', 'wrapper', 'external_ws', 'daemon', 'helper'])
def test_mac_identity_rejects_ambiguous_or_wrong_backend(mac_adapter, change):
    q, _, records = mac_adapter
    if change == 'missing_main':
        records.pop(0)
    elif change == 'duplicate':
        records.append((102, *records[1][1:]))
    elif change == 'helper':
        records[0][4].append(b'--type=renderer')
    elif change in ('listen', 'remote'):
        records[1][4].append(b'--' + change.encode())
    elif change == 'indirect':
        records[1] = (101, 999, *records[1][2:])
    else:
        values = {'wrong_home': b'CODEX_HOME=/elsewhere', 'wrapper': b'CODEX_CLI_PATH=/tmp/wrapper',
                  'external_ws': b'CODEX_APP_SERVER_WS_URL=ws://127.0.0.1:1234',
                  'daemon': b'CODEX_APP_SERVER_USE_LOCAL_DAEMON=1'}
        records[1] = (*records[1][:-1], records[1][-1] + b'\0' + values[change])
    assert q.desktop_running() is False


def test_mac_inspection_denied_is_not_desktop_running(mac_adapter, monkeypatch):
    q, _, _ = mac_adapter
    def denied():
        raise OSError('secret must not be exposed')
    monkeypatch.setattr(macos, 'MacOSProcesses', denied)
    assert q.desktop_running() is False


def test_mac_wake_pins_bundle_and_only_uses_uuid(mac_adapter, monkeypatch):
    q, binding, _ = mac_adapter
    calls = []
    def run(command, **kwargs):
        calls.append((command, kwargs))
        return subprocess.CompletedProcess(command, 0)
    monkeypatch.setattr(desktop.subprocess, 'run', run)
    q.wake(binding)
    assert calls[0][0] == ['/usr/bin/open', '-a', str(q.bundle),
                           'codex://threads/wake/' + ALPHA_THREAD]
    assert calls[0][1]['timeout'] == 20


def test_mac_capability_uses_native_queue_help(mac_adapter, monkeypatch):
    q, _, _ = mac_adapter
    calls = []
    path_stat, access = Path.stat, os.access
    monkeypatch.setattr(Path, 'stat', lambda path, **kw:
        SimpleNamespace(st_mode=stat.S_IFREG | 0o755, st_uid=0)
        if path == Path('/usr/bin/open') else path_stat(path, **kw))
    monkeypatch.setattr(os, 'access', lambda path, mode:
        True if path == Path('/usr/bin/open') else access(path, mode))
    def run(command, **kwargs):
        calls.append(command)
        return subprocess.CompletedProcess(command, 0, 'queue --thread <UUID> --message <TEXT>', '')
    monkeypatch.setattr(desktop.subprocess, 'run', run)
    q.check_capability()
    assert calls == [[str(q.binary), 'queue', '--help']]


@pytest.mark.parametrize('change', ['wrong_bundle', 'path_traversal', 'writable_plist', 'path_cli'])
def test_mac_layout_rejected_before_executing_anything(mac_adapter, monkeypatch, change):
    q, _, _ = mac_adapter
    plist = q.bundle / 'Contents/Info.plist'
    data = plistlib.loads(plist.read_bytes())
    if change == 'wrong_bundle':
        data['CFBundleIdentifier'] = 'com.example.other'
    elif change == 'path_traversal':
        data['CFBundleExecutable'] = '../../other'
    plist.write_bytes(plistlib.dumps(data))
    if change == 'writable_plist':
        plist.chmod(0o666)
    binary = q.binary if change != 'path_cli' else q.home / 'codex'
    fresh = desktop.NativeDesktopQueue(binary, q.home)
    def unexpected(*args, **kwargs):
        pytest.fail('invalid application must not execute a subprocess')
    monkeypatch.setattr(desktop.subprocess, 'run', unexpected)
    with pytest.raises(desktop.DesktopQueueError, match='desktop_queue_unavailable'):
        fresh.check_capability()


def test_mac_registered_wrapper_alias_is_accepted_only_inside_selected_app(mac_adapter):
    q, _, records = mac_adapter
    alias = q.bundle / 'Contents/Resources/codex-cli/bin/codex'
    records[1] = (*records[1][:-1], records[1][-1] + b'\0CODEX_CLI_PATH=' + os.fsencode(alias))
    assert q.desktop_running() is True


def test_mac_short_bundle_layout_supported(mac_adapter):
    q, _, _ = mac_adapter
    binary = q.bundle / 'Contents/Resources/codex'
    binary.write_text('#!/bin/sh\nexit 99\n')
    binary.chmod(0o700)
    fresh = desktop.NativeDesktopQueue(binary, q.home)
    assert fresh.bundle == q.bundle and fresh.desktop == q.desktop


def test_macos_default_selects_installed_native_binary(mac_adapter, monkeypatch):
    q, _, _ = mac_adapter
    expected = Path('/Applications/ChatGPT.app/Contents/Resources/codex-cli/CodexCLI.app/Contents/MacOS/codex')
    monkeypatch.setattr(Path, 'is_file', lambda path: path == expected)
    assert desktop.default_codex_binary() == expected


@pytest.mark.skipif(sys.platform != 'darwin', reason='Darwin libproc/sysctl ABI smoke test')
def test_native_darwin_api_can_inspect_own_process_without_exposing_environment():
    reader = macos.MacOSProcesses()
    pid, parent, started = reader._info(os.getpid())
    assert pid == os.getpid() and parent == os.getppid()
    assert started[0] > 0 and 0 <= started[1] < 1_000_000
    # Framework Python on macOS execs Python.app; sys.executable can still
    # point at the venv launcher rather than the kernel's executable path.
    executable = reader._path(pid)
    assert executable.is_absolute() and executable.is_file()
    assert os.access(executable, os.X_OK)
    args, routing = macos.parse_procargs(reader._sysctl(1, 49, pid))
    assert args
    assert all(pair.partition(b'=')[0] in macos.ROUTING_KEYS
               for pair in routing.split(b'\0') if pair)
    assert reader.group_is_empty(2147483647) is True
