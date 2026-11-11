"""One aot serve per state directory, started on demand when no service is installed."""
import os
import subprocess
import sys

import pytest

from agents_over_telegram import serve
from agents_over_telegram.credentials import write_private_json
from conftest import REAL_START_ON_DEMAND
from test_core import env  # noqa: F401 (env is a fixture)


class Spawned:
    def __init__(self):
        self.calls = []

    def __call__(self, argv, **kwargs):
        self.calls.append((argv, kwargs))
        return type('Process', (), {'pid': 4242})()


@pytest.fixture
def live(tmp_path, monkeypatch):
    monkeypatch.setenv('HOME', str(tmp_path / 'home'))
    monkeypatch.setenv('XDG_CONFIG_HOME', str(tmp_path / 'home' / '.config'))
    monkeypatch.setattr(sys, 'platform', 'linux')
    state = tmp_path / 'state'
    state.mkdir(mode=0o700)
    write_private_json(state / 'mode.json', {'mode': 'live'})
    return state


def test_starts_a_detached_collector(live):
    spawn = Spawned()
    result = REAL_START_ON_DEMAND(live, executable='/opt/aot/bin/aot', spawn=spawn, now=lambda: 1000.0)
    assert result == {'started': True, 'pid': 4242}
    argv, kwargs = spawn.calls[0]
    assert argv == ['/opt/aot/bin/aot', '--state-dir', str(live), 'serve']
    assert kwargs['start_new_session'] is True and kwargs['stdin'] == subprocess.DEVNULL
    assert oct((live / serve.LOG).stat().st_mode & 0o777) == '0o600'


def test_a_collector_that_died_is_restarted_at_most_once_a_minute(live):
    spawn = Spawned()
    REAL_START_ON_DEMAND(live, spawn=spawn, now=lambda: 1000.0)
    assert REAL_START_ON_DEMAND(live, spawn=spawn, now=lambda: 1030.0)['reason'] == 'recently_started'
    assert REAL_START_ON_DEMAND(live, spawn=spawn, now=lambda: 1061.0)['started'] is True
    assert len(spawn.calls) == 2


def test_nothing_starts_while_a_serve_runs(live):
    import fcntl
    fd = os.open(live / serve.SERVE_LOCK, os.O_RDWR | os.O_CREAT, 0o600)
    fcntl.flock(fd, fcntl.LOCK_EX)
    try:
        assert serve.running(live) is True
        assert REAL_START_ON_DEMAND(live, spawn=Spawned())['reason'] == 'running'
    finally:
        os.close(fd)
    assert serve.running(live) is False


def test_nothing_starts_with_the_service_installed_or_after_opting_out(live):
    ok = lambda argv, **kw: subprocess.CompletedProcess(argv, 0)
    serve.install(live, run_command=ok)
    assert REAL_START_ON_DEMAND(live, spawn=Spawned())['reason'] == 'service_installed'
    serve.uninstall(live, run_command=ok)
    (live / serve.NO_SERVE).touch()
    assert REAL_START_ON_DEMAND(live, spawn=Spawned())['reason'] == 'opted_out'
    # Installing the service again lifts the opt-out.
    serve.install(live, run_command=ok)
    assert not (live / serve.NO_SERVE).exists()


def test_fake_state_never_starts_a_collector(live):
    write_private_json(live / 'mode.json', {'mode': 'fake'}, overwrite=True)
    assert REAL_START_ON_DEMAND(live, spawn=Spawned())['reason'] == 'not_live'


def test_a_second_serve_waits_for_the_first(env):
    import fcntl
    core, transport, clock = env
    fd = os.open(core.state_dir / serve.SERVE_LOCK, os.O_RDWR | os.O_CREAT, 0o600)
    fcntl.flock(fd, fcntl.LOCK_EX)
    logs, collected = [], []
    core.collect = lambda **kw: collected.append(1) or {'status': 'ok'}
    left = [3]

    def keep_running():
        left[0] -= 1
        return left[0] >= 0
    try:
        assert serve.run(core, core.state_dir, poll_timeout=0, sleep=lambda s: None, keep_running=keep_running,
                         log=logs.append) == 0
    finally:
        os.close(fd)
    assert collected == [] and 'another aot serve runs' in logs[0]


def test_no_serve_setup_writes_the_opt_out(tmp_path, monkeypatch):
    from agents_over_telegram import cli
    from agents_over_telegram.transport import FakeTransport
    monkeypatch.setattr(cli, 'TelegramTransport', lambda token: FakeTransport())
    monkeypatch.setattr(sys.stdin, 'isatty', lambda: True)
    monkeypatch.setattr(cli.getpass, 'getpass', lambda prompt: '123456:ABCDEFGHIJKLMNOPQRSTUVWXYZ_test_only')
    argv = ['--state-dir', tmp_path / 'state', 'setup', '--label', 'Alpha', '--project-dir', tmp_path / 'project',
            '--prepare-only', '--receiver-handoff-confirmed', '--no-serve']
    cli.run(cli.parser().parse_args([str(x) for x in argv]))
    marker = tmp_path / 'state' / serve.NO_SERVE
    assert marker.exists() and oct(marker.stat().st_mode & 0o777) == '0o600'


def test_the_wake_waiter_and_mcp_start_the_collector(tmp_path, monkeypatch):
    from agents_over_telegram import hosts
    started = []
    monkeypatch.setattr(serve, 'start_on_demand', lambda state, **kw: started.append(state))
    hosts._start_collector(tmp_path)
    monkeypatch.setattr(serve, 'start_on_demand', lambda state, **kw: (_ for _ in ()).throw(OSError('boom')))
    hosts._start_collector(tmp_path)  # never raises
    assert started == [tmp_path]
