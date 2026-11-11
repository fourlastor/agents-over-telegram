import pytest

from agents_over_telegram import onboarding as _onboarding

# The real step, for the tests that check it with a fake service manager.
REAL_INSTALL_COLLECTOR = _onboarding._install_collector
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
