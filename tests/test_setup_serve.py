"""Setup installs the always-on collector only when the human chooses it."""
import subprocess
import sys

import pytest

from agents_over_telegram import cli, onboarding, serve
from agents_over_telegram.transport import FakeTransport
from conftest import REAL_ASK_SERVE, REAL_INSTALL_COLLECTOR


@pytest.fixture
def home(tmp_path, monkeypatch):
    monkeypatch.setenv('HOME', str(tmp_path / 'home'))
    monkeypatch.setenv('XDG_CONFIG_HOME', str(tmp_path / 'home' / '.config'))
    monkeypatch.setattr(sys, 'platform', 'linux')
    monkeypatch.setattr('shutil.which', lambda name: '/usr/bin/' + name)
    state = tmp_path / 'state'
    state.mkdir(mode=0o700)
    return state


def ok(argv, **kw):
    return subprocess.CompletedProcess(argv, 0)


def test_installs_the_service_once(home):
    first = REAL_INSTALL_COLLECTOR(home, run_command=ok, executable='/opt/aot/bin/aot')
    assert first['installed'] is True
    assert first['remove_with'] == f'"/opt/aot/bin/aot" --state-dir "{home}" serve --uninstall'
    assert serve.status(home)['installed'] is True
    again = REAL_INSTALL_COLLECTOR(home, run_command=ok, executable='/opt/aot/bin/aot')
    assert again['already_installed'] is True and again['service'] == serve.name(home)


def test_a_failed_install_never_fails_setup(home):
    commands = []

    def refuse(argv, **kw):
        commands.append(argv)
        if kw.get('check'):
            raise subprocess.CalledProcessError(1, argv)
        return subprocess.CompletedProcess(argv, 1)
    result = REAL_INSTALL_COLLECTOR(home, run_command=refuse, executable='/opt/aot/bin/aot')
    assert result['installed'] is False and result['reason'] == 'CalledProcessError'
    assert f'"/opt/aot/bin/aot" --state-dir "{home}" serve --install' in result['next_step']
    # The cleanup really asks the service manager to undo a partial install.
    assert ['systemctl', '--user', 'disable', '--now', f'{serve.name(home)}.service'] in commands
    assert serve.pointer(home) is None and not (home / serve.LAUNCHER).exists()
    assert serve.status(home)['installed'] is False


def test_unsupported_platforms_are_skipped(home, monkeypatch):
    monkeypatch.setattr(sys, 'platform', 'freebsd14')
    assert REAL_INSTALL_COLLECTOR(home, run_command=ok) == {'installed': False, 'reason': 'unsupported_platform'}
    monkeypatch.setattr(sys, 'platform', 'linux')
    monkeypatch.setattr('shutil.which', lambda name: None)
    assert REAL_INSTALL_COLLECTOR(home, run_command=ok)['reason'] == 'unsupported_platform'


def live_setup(tmp_path, monkeypatch, *extra, ask=False):
    calls = []
    monkeypatch.setattr(onboarding, '_install_collector', lambda state, run_command=None, executable=None:
                        calls.append(state) or {'installed': True, 'service': 'svc'})
    monkeypatch.setattr(onboarding, '_ask_serve', lambda: ask)
    monkeypatch.setattr(cli, 'TelegramTransport', lambda token: FakeTransport())
    monkeypatch.setattr(sys.stdin, 'isatty', lambda: True)
    monkeypatch.setattr(cli.getpass, 'getpass', lambda prompt: '123456:ABCDEFGHIJKLMNOPQRSTUVWXYZ_test_only')
    argv = ['--state-dir', tmp_path / 'state', 'setup', '--label', 'Alpha', '--project-dir', tmp_path / 'project',
            '--prepare-only', '--receiver-handoff-confirmed', *extra]
    return cli.run(cli.parser().parse_args([str(x) for x in argv])), calls


def test_an_agent_running_setup_only_gets_the_offer(tmp_path, monkeypatch):
    result, calls = live_setup(tmp_path, monkeypatch)
    collector = result['always_on_collector']
    assert calls == [] and collector['installed'] is False
    assert 'Ask the human' in collector['offer'] and '--state-dir' in collector['install_with']


def test_serve_installs_the_collector(tmp_path, monkeypatch):
    result, calls = live_setup(tmp_path, monkeypatch, '--serve')
    assert calls == [tmp_path / 'state'] and result['always_on_collector'] == {'installed': True, 'service': 'svc'}


def test_no_serve_declines_without_asking(tmp_path, monkeypatch):
    result, calls = live_setup(tmp_path, monkeypatch, '--no-serve')
    assert calls == [] and result['always_on_collector'] == {'installed': False, 'declined': True}


def test_serve_and_no_serve_exclude_each_other():
    with pytest.raises(SystemExit):
        cli.parser().parse_args(['setup', '--label', 'A', '--project-dir', '/p', '--serve', '--no-serve'])


def test_the_wizard_asks_the_human(monkeypatch, capsys):
    monkeypatch.setattr('builtins.input', lambda: 'yes')
    assert REAL_ASK_SERVE() is True
    monkeypatch.setattr('builtins.input', lambda: 'no')
    assert REAL_ASK_SERVE() is False
    assert 'login service' in capsys.readouterr().err


def test_fake_setup_never_installs(tmp_path, monkeypatch):
    calls = []
    monkeypatch.setattr(onboarding, '_install_collector', lambda state, **kw: calls.append(state))
    result = cli.run(cli.parser().parse_args(['--state-dir', str(tmp_path / 'state'), '--fake', 'setup', '--label',
                                              'Alpha', '--project-dir', str(tmp_path / 'project'), '--prepare-only',
                                              '--serve']))
    assert calls == [] and 'always_on_collector' not in result


def test_the_service_never_moves_to_an_older_runtime(tmp_path, monkeypatch):
    from agents_over_telegram.credentials import write_private_json
    state = tmp_path / 'state'
    state.mkdir(mode=0o700)
    newer, older = tmp_path / 'new' / 'aot', tmp_path / 'older' / 'aot'
    write_private_json(state / serve.POINTER, {'version': 1, 'executable': str(newer), 'aot_version': '99.0.0'})
    assert serve.follow(state, tmp_path / 'old' / 'aot') is False
    assert serve.pointer(state) == newer
    write_private_json(state / serve.POINTER, {'version': 1, 'executable': str(older), 'aot_version': '0.1.0'},
                       overwrite=True)
    assert serve.follow(state, tmp_path / 'this' / 'aot') is True
    assert serve.pointer_version(state) == serve._pointer_value('x')['aot_version']


def test_a_sandbox_that_blocks_the_service_manager_leaves_nothing_behind(home):
    def blocked(argv, **kw):
        raise PermissionError('sandbox')
    result = REAL_INSTALL_COLLECTOR(home, run_command=blocked, executable='/opt/aot/bin/aot')
    assert result['installed'] is False and result['reason'] == 'PermissionError'
    assert serve.pointer(home) is None and not (home / serve.LAUNCHER).exists()
    assert not serve._unit_path(home).exists()
    # So the next setup does not think it is installed.
    assert serve.status(home)['installed'] is False


def test_no_offer_where_no_service_can_be_installed(tmp_path, monkeypatch):
    monkeypatch.setattr('shutil.which', lambda name: None)
    monkeypatch.setattr(sys, 'platform', 'linux')
    result, calls = live_setup(tmp_path, monkeypatch)
    assert result['always_on_collector'] == {'installed': False, 'reason': 'unsupported_platform'}
