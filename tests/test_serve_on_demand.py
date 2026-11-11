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
    assert argv == ['/opt/aot/bin/aot', '--state-dir', str(live), 'serve', '--on-demand']
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
        # A running collector with no record (the service, or one of unknown age) is left alone.
        assert REAL_START_ON_DEMAND(live, spawn=Spawned(), now=lambda: 1000.0)['reason'] == 'running'
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


class Ticks:
    def __init__(self):
        self.now = 0.0

    def __call__(self):
        return self.now


def test_an_on_demand_collector_stops_when_it_should(live, monkeypatch):
    ticks = Ticks()

    class Relay:
        last_activity = None
    relay = Relay()
    write_private_json(live / serve.ON_DEMAND, {'version': 1, 'started': 0, 'executable': str(serve.own_executable())})
    assert serve._on_demand_done(live, relay, 0.0, ticks) is None
    relay.last_activity = 100.0
    ticks.now = 100 + serve.ON_DEMAND_IDLE - 1
    assert serve._on_demand_done(live, relay, 0.0, ticks) is None
    ticks.now = 100 + serve.ON_DEMAND_IDLE
    assert 'no agent' in serve._on_demand_done(live, relay, 0.0, ticks)
    ticks.now = 200.0
    (live / serve.NO_SERVE).touch()
    assert 'opted out' in serve._on_demand_done(live, relay, 0.0, ticks)
    (live / serve.NO_SERVE).unlink()
    write_private_json(live / serve.ON_DEMAND, {'version': 1, 'started': 0, 'executable': '/newer/aot'},
                       overwrite=True)
    assert 'newer runtime' in serve._on_demand_done(live, relay, 0.0, ticks)
    write_private_json(live / serve.ON_DEMAND, {'version': 1, 'started': 0, 'executable': str(serve.own_executable())},
                       overwrite=True)
    serve.install(live, run_command=lambda argv, **kw: subprocess.CompletedProcess(argv, 0))
    assert 'service is installed' in serve._on_demand_done(live, relay, 0.0, ticks)


def test_the_serve_loop_stops_an_idle_on_demand_collector(env, monkeypatch):
    core, transport, clock = env
    ticks = Ticks()
    monkeypatch.setattr(core, 'collect', lambda **kw: ticks.__setattr__('now', ticks.now + serve.ON_DEMAND_IDLE) or
                        {'status': 'ok'})
    monkeypatch.setattr(core, 'flush', lambda **kw: None)
    logs = []
    assert serve.run(core, core.state_dir, poll_timeout=0, sleep=lambda s: None, log=logs.append,
                     on_demand=True, clock=ticks) == 0
    assert 'started on demand' in logs[0] and 'no agent used it' in logs[-1]


def test_an_on_demand_collector_gives_up_waiting_for_the_lock(env):
    import fcntl
    core, transport, clock = env
    ticks = Ticks()
    fd = os.open(core.state_dir / serve.SERVE_LOCK, os.O_RDWR | os.O_CREAT, 0o600)
    fcntl.flock(fd, fcntl.LOCK_EX)
    try:
        def sleep(seconds):
            ticks.now += seconds
        assert serve.run(core, core.state_dir, poll_timeout=0, sleep=sleep, log=lambda t: None,
                         on_demand=True, clock=ticks) == 0
        assert ticks.now >= serve.ON_DEMAND_LOCK_WAIT
    finally:
        os.close(fd)


def test_a_newer_runtime_replaces_an_older_on_demand_collector(live):
    import fcntl
    write_private_json(live / serve.ON_DEMAND, {'version': 1, 'started': 0, 'executable': '/old/aot',
                                                'aot_version': '0.1.0'})
    fd = os.open(live / serve.SERVE_LOCK, os.O_RDWR | os.O_CREAT, 0o600)
    fcntl.flock(fd, fcntl.LOCK_EX)
    try:
        spawn = Spawned()
        result = REAL_START_ON_DEMAND(live, executable='/new/aot', spawn=spawn, now=lambda: 10.0)
        assert result['started'] is True
        # The record now names the new runtime, so the old collector stops.
        assert serve._on_demand_record(live)['executable'] == '/new/aot'
        # The same runtime again, or an older one, starts nothing.
        assert REAL_START_ON_DEMAND(live, executable='/new/aot', spawn=spawn, now=lambda: 500.0)['reason'] == 'running'
        assert REAL_START_ON_DEMAND(live, executable='/old/aot', spawn=spawn, now=lambda: 900.0)['reason'] == 'running'
    finally:
        os.close(fd)


def test_two_agents_starting_at_once_spawn_one(live):
    import fcntl
    fd = os.open(live / serve.ON_DEMAND_LOCK, os.O_RDWR | os.O_CREAT, 0o600)
    fcntl.flock(fd, fcntl.LOCK_EX)
    try:
        assert REAL_START_ON_DEMAND(live, spawn=Spawned())['reason'] == 'starting'
    finally:
        os.close(fd)


def test_the_started_process_is_reaped(live):
    waited = []

    class Process:
        pid = 7

        def wait(self):
            waited.append(1)
    REAL_START_ON_DEMAND(live, spawn=lambda argv, **kw: Process())
    import time
    deadline = time.monotonic() + 5
    while not waited and time.monotonic() < deadline:
        time.sleep(0.01)
    assert waited == [1]


def test_a_failed_start_leaves_a_line_in_the_log(tmp_path, monkeypatch):
    from agents_over_telegram import hosts
    monkeypatch.setattr(serve, 'start_on_demand', lambda state, **kw: (_ for _ in ()).throw(PermissionError('sandbox')))
    hosts._start_collector(tmp_path)
    assert 'could not start aot serve on demand (PermissionError: sandbox)' in (tmp_path / serve.LOG).read_text()


def test_uninstalling_the_service_also_stops_on_demand_starts(env, monkeypatch):
    from agents_over_telegram.cli import parser, run
    core, transport, clock = env
    monkeypatch.setattr(serve, 'uninstall', lambda state, **kw: {'installed': False, 'removed': True})
    result = run(parser().parse_args(['--state-dir', str(core.state_dir), '--fake', 'serve', '--uninstall']))
    assert result == {'installed': False, 'removed': True, 'on_demand': False}
    assert (core.state_dir / serve.NO_SERVE).exists()
