"""The release workflow's tag check and the npm package contents."""
import json
from pathlib import Path
import shutil
import sys

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'scripts'))
import release_check  # noqa: E402


def test_the_current_version_files_agree():
    from agents_over_telegram import __version__
    assert set(release_check.versions().values()) == {__version__}
    assert release_check.problems(f'v{__version__}') == []


@pytest.mark.parametrize('tag', ['0.11.0', 'v0.11', 'v0.11.0-rc1', 'vx.y.z'])
def test_a_malformed_tag_is_refused(tag):
    assert 'is not of the form vX.Y.Z' in release_check.problems(tag)[0]


def test_a_version_file_that_differs_is_named(tmp_path):
    for name in [*release_check.JSON_FILES, 'pyproject.toml', 'src/agents_over_telegram/__init__.py', 'uv.lock',
                 'README.md']:
        (tmp_path / name).parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(ROOT / name, tmp_path / name)
    manifest = tmp_path / '.codex-plugin' / 'plugin.json'
    value = json.loads(manifest.read_text(encoding='utf-8'))
    from agents_over_telegram import __version__
    value['version'] = '0.0.1'
    manifest.write_text(json.dumps(value), encoding='utf-8')
    [problem] = release_check.problems(f'v{__version__}', tmp_path)
    assert problem.startswith('.codex-plugin/plugin.json has version')


def test_the_lock_file_and_the_readme_are_checked():
    assert {'uv.lock', 'README.md'} <= set(release_check.versions())


def test_the_npm_package_carries_what_the_bootstrap_and_pi_need():
    package = json.loads((ROOT / 'package.json').read_text(encoding='utf-8'))
    assert 'private' not in package and 'pi-package' in package['keywords']
    files = package['files']
    # bootstrap_runtime.py builds the runtime from these files (snapshot_source).
    for needed in ('scripts/bootstrap_runtime.py', 'pyproject.toml', 'uv.lock', 'LICENSE',
                   'requirements/runtime.txt', 'requirements/build.txt', 'src/agents_over_telegram/**/*.py'):
        assert needed in files
    # The skills link to the docs, and Pi loads the extension and the skills.
    assert 'docs/' in files and 'skills/' in files
    assert all(path.removeprefix('./') in files or path.removeprefix('./') + '/' in files
               for path in package['pi']['extensions'] + package['pi']['skills'])
