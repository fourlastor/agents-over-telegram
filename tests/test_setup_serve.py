"""Setup installs the always-on collector by default; --no-serve skips it."""
import subprocess
import sys

import pytest

from agents_over_telegram import cli, onboarding, serve
from agents_over_telegram.transport import FakeTransport
from conftest import REAL_INSTALL_COLLECTOR


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
    first = REAL_INSTALL_COLLECTOR(home, run_command=ok)
    assert first['installed'] is True and first['remove_with'] == 'aot serve --uninstall'
    assert serve.status(home)['installed'] is True
    assert REAL_INSTALL_COLLECTOR(home, run_command=ok) == {'installed': True, 'already_installed': True,
                                                             'service': serve.name(home)}


def test_a_failed_install_never_fails_setup(home):
    def refuse(argv, **kw):
        raise subprocess.CalledProcessError(1, argv)
    result = REAL_INSTALL_COLLECTOR(home, run_command=refuse)
    assert result['installed'] is False and result['reason'] == 'CalledProcessError'
    assert 'aot serve --install' in result['next_step']
    assert serve.pointer(home) is None and not (home / serve.LAUNCHER).exists()
    assert serve.status(home)['installed'] is False


def test_unsupported_platforms_are_skipped(home, monkeypatch):
    monkeypatch.setattr(sys, 'platform', 'freebsd14')
    assert REAL_INSTALL_COLLECTOR(home, run_command=ok) == {'installed': False, 'reason': 'unsupported_platform'}
    monkeypatch.setattr(sys, 'platform', 'linux')
    monkeypatch.setattr('shutil.which', lambda name: None)
    assert REAL_INSTALL_COLLECTOR(home, run_command=ok)['reason'] == 'unsupported_platform'


def live_setup(tmp_path, monkeypatch, *extra):
    calls = []
    monkeypatch.setattr(onboarding, '_install_collector', lambda state, run_command=None: calls.append(state) or
                        {'installed': True, 'service': 'svc'})
    monkeypatch.setattr(cli, 'TelegramTransport', lambda token: FakeTransport())
    monkeypatch.setattr(sys.stdin, 'isatty', lambda: True)
    monkeypatch.setattr(cli.getpass, 'getpass', lambda prompt: '123456:ABCDEFGHIJKLMNOPQRSTUVWXYZ_test_only')
    argv = ['--state-dir', tmp_path / 'state', 'setup', '--label', 'Alpha', '--project-dir', tmp_path / 'project',
            '--prepare-only', '--receiver-handoff-confirmed', *extra]
    return cli.run(cli.parser().parse_args([str(x) for x in argv])), calls


def test_live_setup_installs_the_collector(tmp_path, monkeypatch):
    result, calls = live_setup(tmp_path, monkeypatch)
    assert calls == [tmp_path / 'state'] and result['always_on_collector'] == {'installed': True, 'service': 'svc'}


def test_no_serve_skips_the_install(tmp_path, monkeypatch):
    result, calls = live_setup(tmp_path, monkeypatch, '--no-serve')
    assert calls == [] and result['always_on_collector'] == {'installed': False, 'skipped': 'no_serve'}


def test_fake_setup_never_installs(tmp_path, monkeypatch):
    calls = []
    monkeypatch.setattr(onboarding, '_install_collector', lambda state, run_command=None: calls.append(state))
    result = cli.run(cli.parser().parse_args(['--state-dir', str(tmp_path / 'state'), '--fake', 'setup', '--label',
                                              'Alpha', '--project-dir', str(tmp_path / 'project'), '--prepare-only']))
    assert calls == [] and 'always_on_collector' not in result
