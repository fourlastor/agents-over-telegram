"""Installer-independent exports preserve the reviewed dependency lock."""
from importlib.util import module_from_spec, spec_from_file_location
from pathlib import Path
import shutil

import pytest


ROOT = Path(__file__).resolve().parents[1]
spec = spec_from_file_location("export_requirements", ROOT / "scripts" / "export_requirements.py")
exports = module_from_spec(spec)
spec.loader.exec_module(exports)


@pytest.fixture
def package(tmp_path):
    for name in ("pyproject.toml", "uv.lock"):
        shutil.copy(ROOT / name, tmp_path / name)
    shutil.copytree(ROOT / "requirements", tmp_path / "requirements")
    return tmp_path


def test_export_validation_works_without_uv_or_subprocesses(package, monkeypatch):
    def forbidden(*args, **kwargs):
        pytest.fail("Packaged export validation must not invoke an installer")
    monkeypatch.setattr(exports.subprocess, "run", forbidden)
    exports.validate_exports(package)


@pytest.mark.parametrize("name", ["pyproject.toml", "uv.lock"])
def test_source_changes_require_regenerated_exports(package, name):
    path = package / name
    path.write_text(path.read_text() + "\n")
    with pytest.raises(ValueError, match="requirements are stale"):
        exports.validate_exports(package)


@pytest.mark.parametrize("kind", ["runtime", "build"])
def test_changed_artifact_hash_is_rejected(package, kind):
    path = package / "requirements" / f"{kind}.txt"
    contents = path.read_text()
    start = contents.index("--hash=sha256:") + len("--hash=sha256:")
    path.write_text(contents[:start] + "0" * 64 + contents[start + 64:])
    with pytest.raises(ValueError, match="hashes differ from the lock"):
        exports.validate_exports(package)


@pytest.mark.parametrize("replacement", ["hatchling>=1.27.0", "hatchling==1.27.0 --extra-index-url https://example.invalid"])
def test_unpinned_or_option_bearing_requirements_are_rejected(package, replacement):
    path = package / "requirements" / "build.txt"
    path.write_text(path.read_text().replace("hatchling==1.27.0", replacement))
    with pytest.raises(ValueError, match="only exact pins"):
        exports.validate_exports(package)


def test_missing_transitive_build_dependency_is_rejected(package):
    path = package / "requirements" / "build.txt"
    contents = path.read_text()
    start = contents.index("pathspec==")
    end = contents.index("pluggy==", start)
    path.write_text(contents[:start] + contents[end:])
    with pytest.raises(ValueError, match="omit or add locked dependencies"):
        exports.validate_exports(package)


def test_build_group_cannot_drift_from_backend_requirement(package):
    path = package / "pyproject.toml"
    path.write_text(path.read_text().replace('bootstrap-build = ["hatchling==1.27.0"]', 'bootstrap-build = ["hatchling==1.26.0"]'))
    with pytest.raises(ValueError, match="must match the project build requirements"):
        exports.validate_exports(package)


@pytest.mark.parametrize("kind", ["runtime", "build"])
def test_requirements_symlink_is_rejected(package, kind):
    path = package / "requirements" / f"{kind}.txt"
    path.unlink()
    path.symlink_to(ROOT / "requirements" / f"{kind}.txt")
    with pytest.raises(ValueError, match="requirements are missing or unsafe"):
        exports.validate_exports(package)
