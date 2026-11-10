"""aot serve: the always-on collector, its user service and its runtime pointer."""
import json
import os
from pathlib import Path
import subprocess
import sys

import pytest

from agents_over_telegram import serve
from agents_over_telegram.credentials import write_private_json
from agents_over_telegram.transport import TransportError
from test_core import append, bind, env  # noqa: F401 (env is a fixture)
from test_settings_panel import query


def loop(core, state, cycles, **kwargs):
    """Run the serve loop for a fixed number of cycles."""
    left = [cycles]

    def keep_running():
        left[0] -= 1
        return left[0] >= 0
    logs, pauses = [], []
    code = serve.run(core, state, poll_timeout=0, sleep=pauses.append, keep_running=keep_running,
                     log=logs.append, **kwargs)
    return code, logs, pauses


def test_serve_collects_and_answers_owner_actions_on_its_own(env):
    core, transport, clock = env
    client, _ = bind(core, transport)
    message_id = core.send_settings_panel(client)['message_id']
    append(transport, 'hello from the group')
    q = query(transport, message_id, f's:{client.removeprefix("agent_")}:0:1')
    code, logs, pauses = loop(core, core.state_dir, 1)
    assert code == 0 and logs[0].startswith('aot serve: collecting')
    # The panel tap was applied and answered without any agent polling.
    assert core.delivery_settings(client)['delivery_filter'] == 'addressed'
    assert [a['id'] for a in transport.widget_answers] == [q]
    assert [d['text'] for d in core.poll(client)['deliveries']] == ['hello from the group']


def test_serve_judges_and_flushes(env, monkeypatch):
    core, transport, clock = env
    calls = []
    monkeypatch.setattr(core, 'collect', lambda **kw: calls.append(('collect', kw)) or {'status': 'ok'})
    monkeypatch.setattr(core, 'flush', lambda **kw: calls.append(('flush', kw)))
    loop(core, core.state_dir, 2)
    assert calls == [('collect', {'timeout': 0, 'judge': True}), ('flush', {'limit': 20})] * 2


def test_serve_waits_while_another_collector_holds_the_lock(env, monkeypatch):
    core, transport, clock = env
    monkeypatch.setattr(core, 'collect', lambda **kw: {'status': 'busy', 'updates': 0})
    code, logs, pauses = loop(core, core.state_dir, 2)
    assert pauses == [serve.BUSY_PAUSE] * 2


def test_serve_backs_off_on_failures_and_recovers(env, monkeypatch):
    core, transport, clock = env
    outcomes = [TransportError('transport_failure'), TransportError('transport_failure'), {'status': 'ok'}]

    def collect(**kw):
        outcome = outcomes.pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        return outcome
    monkeypatch.setattr(core, 'collect', collect)
    monkeypatch.setattr(core, 'flush', lambda **kw: None)
    code, logs, pauses = loop(core, core.state_dir, 3)
    assert pauses == [serve.BACKOFF, serve.BACKOFF * 2]
    assert logs[-1] == 'aot serve: collecting again'
    assert 'transport_failure' in logs[1]


def test_serve_restarts_when_a_newer_runtime_is_installed(env, tmp_path):
    core, transport, clock = env
    write_private_json(core.state_dir / serve.POINTER, {'version': 1, 'executable': str(tmp_path / 'other' / 'aot')})
    code, logs, pauses = loop(core, core.state_dir, 5)
    assert code == 3 and 'newer runtime' in logs[-1]


def test_serve_keeps_running_on_its_own_runtime(env, monkeypatch):
    core, transport, clock = env
    write_private_json(core.state_dir / serve.POINTER, {'version': 1, 'executable': str(serve.own_executable())})
    monkeypatch.setattr(core, 'collect', lambda **kw: {'status': 'ok'})
    monkeypatch.setattr(core, 'flush', lambda **kw: None)
    assert loop(core, core.state_dir, 3)[0] == 0


def test_follow_moves_only_an_installed_service(tmp_path):
    state = tmp_path / 'state'
    state.mkdir(mode=0o700)
    assert serve.follow(state, tmp_path / 'new' / 'aot') is False
    assert not (state / serve.POINTER).exists()
    write_private_json(state / serve.POINTER, {'version': 1, 'executable': str(tmp_path / 'old' / 'aot')})
    assert serve.follow(state, tmp_path / 'new' / 'aot') is True
    assert serve.pointer(state) == tmp_path / 'new' / 'aot'
    assert serve.follow(state, tmp_path / 'new' / 'aot') is False


@pytest.fixture
def home(tmp_path, monkeypatch):
    monkeypatch.setenv('HOME', str(tmp_path / 'home'))
    monkeypatch.setenv('XDG_CONFIG_HOME', str(tmp_path / 'home' / '.config'))
    monkeypatch.setattr(sys, 'platform', 'linux')
    state = tmp_path / 'state'
    state.mkdir(mode=0o700)
    return state


def test_install_and_uninstall_a_systemd_user_service(home):
    commands = []

    def run_command(argv, **kw):
        commands.append(argv)
        return subprocess.CompletedProcess(argv, 0)
    result = serve.install(home, run_command=run_command, python='/usr/bin/python3')
    service = serve.name(home)
    unit = home.parent / 'home' / '.config' / 'systemd' / 'user' / f'{service}.service'
    assert result['unit'] == str(unit) and result['service'] == service
    text = unit.read_text()
    assert f'ExecStart="/usr/bin/python3" "{home / serve.LAUNCHER}"' in text and 'Restart=always' in text
    assert serve.pointer(home) == serve.own_executable()
    assert oct((home / serve.LAUNCHER).stat().st_mode & 0o777) == '0o600'
    assert ['systemctl', '--user', 'enable', '--now', f'{service}.service'] in commands
    assert serve.status(home)['installed'] is True
    assert serve.uninstall(home, run_command=run_command) == {'installed': False, 'removed': True}
    assert not unit.exists() and not (home / serve.LAUNCHER).exists() and serve.pointer(home) is None
    assert ['systemctl', '--user', 'disable', '--now', f'{service}.service'] in commands


def test_install_a_launchd_agent_on_macos(home, monkeypatch):
    monkeypatch.setattr(sys, 'platform', 'darwin')
    commands = []
    result = serve.install(home, run_command=lambda argv, **kw: commands.append(argv), python='/usr/bin/python3')
    import plistlib
    plist = Path(result['unit'])
    assert plist.parent == home.parent / 'home' / 'Library' / 'LaunchAgents'
    value = plistlib.loads(plist.read_bytes())
    assert value['ProgramArguments'] == ['/usr/bin/python3', str(home / serve.LAUNCHER)]
    assert value['KeepAlive'] is True and value['Label'].startswith('dev.agents-over-telegram.serve.')
    assert commands[-1][:2] == ['launchctl', 'bootstrap']


def test_the_launcher_runs_the_recorded_executable(tmp_path):
    state = tmp_path / 'state'
    state.mkdir(mode=0o700)
    fake = tmp_path / 'aot'
    out = tmp_path / 'argv.json'
    fake.write_text(f'#!{sys.executable}\nimport json, sys\nopen({str(out)!r}, "w").write(json.dumps(sys.argv[1:]))\n')
    fake.chmod(0o700)
    (state / serve.LAUNCHER).write_text(serve.LAUNCHER_SOURCE)
    write_private_json(state / serve.POINTER, {'version': 1, 'executable': str(fake)})
    subprocess.run([sys.executable, str(state / serve.LAUNCHER)], check=True, timeout=30)
    assert json.loads(out.read_text()) == ['--state-dir', str(state), 'serve']


def test_the_cli_reports_status(env):
    from agents_over_telegram.cli import parser, run
    core, transport, clock = env
    result = run(parser().parse_args(['--state-dir', str(core.state_dir), '--fake', 'serve', '--status']))
    assert result['installed'] is False and result['executable'] is None
    with pytest.raises(SystemExit):
        parser().parse_args(['serve', '--install', '--uninstall'])


def test_two_state_directories_get_separate_services(home):
    other = home.parent / 'other-state'
    other.mkdir(mode=0o700)
    ok = lambda argv, **kw: subprocess.CompletedProcess(argv, 0)
    first, second = serve.install(home, run_command=ok), serve.install(other, run_command=ok)
    assert first['unit'] != second['unit'] and first['service'] != second['service']
    assert serve.status(home)['installed'] is True and serve.status(other)['installed'] is True
    serve.uninstall(other, run_command=ok)
    assert serve.status(home)['installed'] is True and serve.status(other)['installed'] is False
    assert Path(first['unit']).exists()


def test_status_needs_a_unit_that_starts_this_launcher(home):
    write_private_json(home / serve.POINTER, {'version': 1, 'executable': str(serve.own_executable())})
    assert serve.status(home)['installed'] is False
    unit = Path(serve.status(home)['unit'])
    unit.parent.mkdir(parents=True)
    unit.write_text('ExecStart=/usr/bin/python3 /elsewhere/serve_launch.py')
    assert serve.status(home)['installed'] is False


def test_a_big_log_is_rotated_when_serve_starts(env):
    core, transport, clock = env
    log = core.state_dir / serve.LOG
    log.write_bytes(b'x' * (serve.LOG_LIMIT + 1))
    loop(core, core.state_dir, 0)
    assert not log.exists() and (core.state_dir / (serve.LOG + '.1')).stat().st_size == serve.LOG_LIMIT + 1
