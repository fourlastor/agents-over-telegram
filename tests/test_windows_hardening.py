"""Windows hardening: private folder ACLs, reparse points, lock errors, supervised collectors, busy relays."""
import json
import os
import socket
import subprocess
import sys
import threading
import types

import pytest

from agents_over_telegram import compat, relay_api, serve
from agents_over_telegram.credentials import CredentialError, private_directory, write_private_json
from test_core import env  # noqa: F401 (env is a fixture)


def test_a_private_folder_gets_its_own_acl_on_windows(tmp_path, monkeypatch):
    monkeypatch.setattr(compat, 'WINDOWS', True)
    monkeypatch.setenv('USERNAME', 'ada')
    monkeypatch.setenv('USERDOMAIN', 'HOME')
    calls = []
    assert compat.restrict_directory(tmp_path, run=lambda argv, **kw: calls.append(argv) or
                                     subprocess.CompletedProcess(argv, 0)) is True
    assert calls == [['icacls', str(tmp_path), '/inheritance:r', '/grant:r', 'HOME\\ada:(OI)(CI)F',
                      '*S-1-5-18:(OI)(CI)F']]
    assert compat.restrict_directory(tmp_path, run=lambda argv, **kw: subprocess.CompletedProcess(argv, 5)) is False

    def missing(argv, **kw):
        raise FileNotFoundError('icacls')
    assert compat.restrict_directory(tmp_path, run=missing) is False


def test_posix_folders_need_no_acl(tmp_path):
    assert compat.WINDOWS or compat.restrict_directory(tmp_path, run=lambda *a, **k: 1 / 0) is True


def test_a_new_private_folder_that_cannot_be_restricted_is_refused(tmp_path, monkeypatch):
    monkeypatch.setattr(compat, 'restrict_directory', lambda path, run=None: False)
    with pytest.raises(CredentialError) as error:
        private_directory(tmp_path / 'state')
    assert error.value.code == 'state_directory_acl_failed'
    # An existing folder is not restricted again on every use.
    (tmp_path / 'existing').mkdir(mode=0o700)
    assert private_directory(tmp_path / 'existing') == tmp_path / 'existing'


def test_reparse_points_are_refused_on_windows(tmp_path, monkeypatch):
    monkeypatch.setattr(compat, 'WINDOWS', True)
    plain = tmp_path / 'plain.json'
    plain.write_text('{}', encoding='utf-8', newline='\n')
    compat.refuse_link(plain)
    real = os.lstat(plain)
    junction = types.SimpleNamespace(st_mode=real.st_mode, st_file_attributes=0x400)
    monkeypatch.setattr(compat.os, 'lstat', lambda path: junction)
    with pytest.raises(OSError):
        compat.refuse_link(plain)


@pytest.mark.parametrize('errno, contended', [(13, True), (36, True), (9, False)])
def test_only_lock_contention_counts_as_held(tmp_path, monkeypatch, errno, contended):
    def locking(fd, mode, size):
        raise OSError(errno, 'fake')
    monkeypatch.setitem(sys.modules, 'msvcrt', types.SimpleNamespace(locking=locking, LK_NBLCK=2, LK_UNLCK=0))
    monkeypatch.setattr(compat, 'WINDOWS', True)
    fd = os.open(tmp_path / 'x.lock', os.O_RDWR | os.O_CREAT, 0o600)
    try:
        with pytest.raises(BlockingIOError if contended else OSError) as error:
            compat.lock(fd)
        assert isinstance(error.value, BlockingIOError) == contended
    finally:
        os.close(fd)


def test_a_supervised_collector_stops_when_its_service_is_removed(env, monkeypatch):
    core, transport, clock = env
    monkeypatch.setenv('AOT_SERVE_SUPERVISED', '1')
    monkeypatch.setattr(core, 'collect', lambda **kw: {'status': 'ok'})
    monkeypatch.setattr(core, 'flush', lambda **kw: None)
    logs = []
    assert serve.run(core, core.state_dir, poll_timeout=0, sleep=lambda s: None, log=logs.append) == 0
    assert 'service was removed' in logs[-1]


def test_an_unsupervised_collector_ignores_the_missing_pointer(env, monkeypatch):
    core, transport, clock = env
    monkeypatch.delenv('AOT_SERVE_SUPERVISED', raising=False)
    monkeypatch.setattr(core, 'collect', lambda **kw: {'status': 'ok'})
    monkeypatch.setattr(core, 'flush', lambda **kw: None)
    left = [3]

    def keep_running():
        left[0] -= 1
        return left[0] >= 0
    assert serve.run(core, core.state_dir, poll_timeout=0, sleep=lambda s: None, keep_running=keep_running,
                     log=lambda t: None) == 0
    assert left[0] < 0


def test_the_launcher_marks_the_collector_as_supervised():
    assert "os.environ['AOT_SERVE_SUPERVISED'] = '1'" in serve.LAUNCHER_SOURCE


@pytest.mark.skipif(not hasattr(socket, 'AF_UNIX'), reason='needs Unix sockets')
def test_a_busy_answer_before_reading_a_large_request_still_arrives(env):
    core, transport, clock = env
    path = relay_api.socket_path(core.state_dir)
    private_directory(path.parent)
    server = socket.socket(socket.AF_UNIX)
    server.bind(str(path))
    server.listen(1)

    def answer_busy_without_reading():
        connection, _ = server.accept()
        connection.sendall(json.dumps({'protocol': relay_api.PROTOCOL, 'ok': False, 'error': 'busy'}).encode() + b'\n')
        connection.close()
    thread = threading.Thread(target=answer_busy_without_reading, daemon=True)
    thread.start()
    try:
        with pytest.raises(relay_api.Unavailable) as error:
            relay_api.call(core.state_dir, 'secret', 'message_send',
                           {'text': 'x' * 1_000_000, 'idempotency_key': 'k'}, timeout=10)
        assert str(error.value) == 'busy'
    finally:
        thread.join(5)
        server.close()
