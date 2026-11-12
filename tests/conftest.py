import os

import pytest

# The Codex desktop integrations (the desktop listener, the app-server client,
# the shared-backend wrapper and the relay watcher) drive Unix processes,
# sockets and signals and are not supported on Windows.
collect_ignore = ([
    'test_appserver_client.py', 'test_desktop_dispatcher.py', 'test_dispatcher.py',
    'test_macos_desktop_queue.py', 'test_native_desktop_queue.py', 'test_relay_watch.py',
    'test_shared_backend_wrapper.py',
    # These fake uv and the runtime with POSIX shebang scripts; CI runs the
    # real bootstrap on Windows instead.
    'test_bootstrap_runtime.py',
] if os.name == 'nt' else [])

from agents_over_telegram import core as _core
from agents_over_telegram import onboarding as _onboarding
from agents_over_telegram import serve as _serve

# The production setting, for the test that checks it.
REAL_SQLITE_SYNCHRONOUS = _core.Core.SQLITE_SYNCHRONOUS
# No test cuts the power, so commits need not wait for the disk; on Windows
# that wait made up a third of the suite's time. Child processes the tests
# start keep the production setting.
_core.Core.SQLITE_SYNCHRONOUS = 'NORMAL'

# The real steps, for the tests that check them with fakes.
REAL_INSTALL_COLLECTOR = _onboarding._install_collector
REAL_START_ON_DEMAND = _serve.start_on_demand
REAL_ASK_SERVE = _onboarding._ask_serve


@pytest.fixture(autouse=True)
def _empty_working_directory(tmp_path_factory, monkeypatch):
    # The CLI reads the project profile in the current folder. A checkout that
    # is itself paired has one, so tests start in an empty folder instead of
    # wherever pytest was launched. Tests that need a project folder chdir there.
    monkeypatch.chdir(tmp_path_factory.mktemp('cwd'))


@pytest.fixture(autouse=True)
def _no_real_collector_service(monkeypatch):
    # Live-mode setup installs aot serve as a user service. Tests must never
    # touch this machine's systemd or launchd; tests of that step patch it back.
    from agents_over_telegram import onboarding
    monkeypatch.setattr(onboarding, '_install_collector',
                        lambda state, run_command=None, executable=None: {'installed': False, 'reason': 'test'})
    # The interactive wizard asks the human about it; tests answer no.
    monkeypatch.setattr(onboarding, '_ask_serve', lambda: False)


@pytest.fixture(autouse=True)
def _no_collector_started_on_demand(monkeypatch):
    # Agents start aot serve in the background when no service is installed.
    # Tests must never leave a real process behind; its own tests use the real step.
    from agents_over_telegram import serve
    monkeypatch.setattr(serve, 'start_on_demand', lambda state, **kwargs: {'started': False, 'reason': 'test'})


_HANG_SECONDS = int(os.environ.get('AOT_TEST_HANG_SECONDS') or 0)


@pytest.fixture(autouse=True)
def _hang_guard():
    # CI sets AOT_TEST_HANG_SECONDS where a hung test would otherwise run into
    # the job limit, which discards the whole log. faulthandler dumps every
    # thread's stack and exits the process instead, so the log shows where.
    if not _HANG_SECONDS:
        yield
        return
    import faulthandler
    import sys
    # pytest swaps sys.stderr per test; the dump must outlive that.
    faulthandler.dump_traceback_later(_HANG_SECONDS, exit=True, file=sys.__stderr__)
    try:
        yield
    finally:
        faulthandler.cancel_dump_traceback_later()


# For tests of POSIX file modes, owners and shebang scripts, which Windows does not have.
posix_only = pytest.mark.skipif(os.name == 'nt', reason='POSIX file modes, owners or scripts')
