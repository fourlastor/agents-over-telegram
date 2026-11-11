import pytest

from agents_over_telegram import onboarding as _onboarding
from agents_over_telegram import serve as _serve

# The real steps, for the tests that check them with fakes.
REAL_INSTALL_COLLECTOR = _onboarding._install_collector
REAL_START_ON_DEMAND = _serve.start_on_demand


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
                        lambda state, run_command=None: {'installed': False, 'reason': 'test'})


@pytest.fixture(autouse=True)
def _no_collector_started_on_demand(monkeypatch):
    # Agents start aot serve in the background when no service is installed.
    # Tests must never leave a real process behind; its own tests use the real step.
    from agents_over_telegram import serve
    monkeypatch.setattr(serve, 'start_on_demand', lambda state, **kwargs: {'started': False, 'reason': 'test'})
