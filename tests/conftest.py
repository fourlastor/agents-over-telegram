import pytest


@pytest.fixture(autouse=True)
def _empty_working_directory(tmp_path_factory, monkeypatch):
    # The CLI reads the project profile in the current folder. A checkout that
    # is itself paired has one, so tests start in an empty folder instead of
    # wherever pytest was launched. Tests that need a project folder chdir there.
    monkeypatch.chdir(tmp_path_factory.mktemp('cwd'))
