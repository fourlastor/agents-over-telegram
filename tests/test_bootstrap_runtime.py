"""Installed-cache bootstrap isolation, interruption handling and stable executable paths."""
from importlib.util import module_from_spec, spec_from_file_location
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys

import pytest

ROOT = Path(__file__).resolve().parents[1]
spec = spec_from_file_location("bootstrap_runtime", ROOT / "scripts" / "bootstrap_runtime.py")
bootstrap = module_from_spec(spec)
spec.loader.exec_module(bootstrap)
REAL_RUN_CHECKED = bootstrap.run_checked


@pytest.fixture
def package(tmp_path):
    root = tmp_path / "plugin-cache"
    source = root / "src" / "agents_over_telegram"
    source.mkdir(parents=True)
    (root / "pyproject.toml").write_text('[project]\nname = "agents-over-telegram"\nversion = "0.7.0"\n')
    (root / "uv.lock").write_text("version = 1\n")
    (root / "LICENSE").write_text("License fixture\n")
    (root / "requirements").mkdir()
    (root / "requirements" / "runtime.txt").write_text("# hashed runtime requirements\n")
    (root / "requirements" / "build.txt").write_text("# hashed build requirements\n")
    (source / "__init__.py").write_text('__version__ = "0.7.0"\n')
    return root


@pytest.fixture
def fake_uv(tmp_path):
    directory = tmp_path / "tools"
    directory.mkdir()
    executable = directory / "uv"
    executable.write_text(f'''#!{sys.executable}
import json, os, pathlib, sys
root = pathlib.Path(sys.argv[sys.argv.index("--project") + 1])
(root / "uv-observation.json").write_text(json.dumps({{"args": sys.argv[1:], "env": dict(os.environ)}}))
binary = root / ".venv" / "bin"
binary.mkdir(parents=True, exist_ok=True)
for name, output in (("aot", "usage: aot setup relay-desktop"), ("python", "0.7.0")):
    path = binary / name
    path.write_text("#!{sys.executable}\\nprint(" + repr(output) + ")\\n")
    path.chmod(0o700)
''')
    executable.chmod(0o700)
    return executable


def environment(fake_uv):
    return {"PATH": str(fake_uv.parent), "UV_PROJECT_ENVIRONMENT": "/outside/venv",
            "VIRTUAL_ENV": "/outside/active", "PYTHONPATH": "/outside/imports",
            "UV_INDEX_PASSWORD": "DO_NOT_ECHO", "AOT_BOT_TOKEN": "DO_NOT_ECHO",
            "AWS_SECRET_ACCESS_KEY": "DO_NOT_ECHO", "HOME": "/outside/home"}


def test_install_isolated_and_stable_after_plugin_cache_removal(tmp_path, package, fake_uv):
    result = bootstrap.bootstrap(package, tmp_path / "runtimes", offline=True, environ=environment(fake_uv))
    assert result["status"] == "ready" and result["version"] == "0.7.0"
    assert result["reused"] is False
    runtime = Path(result["runtime_dir"])
    observation = json.loads((runtime / "uv-observation.json").read_text())
    args, env = observation["args"], observation["env"]
    for flag in ("--frozen", "--no-dev", "--no-default-groups", "--no-editable", "--offline", "--no-config", "--no-python-downloads", "--no-managed-python"):
        assert flag in args
    assert args[args.index("--python") + 1] == str(Path(sys.executable).resolve())
    assert args[args.index("--keyring-provider") + 1] == "disabled"
    for key in ("UV_PROJECT_ENVIRONMENT", "VIRTUAL_ENV", "PYTHONPATH", "UV_INDEX_PASSWORD", "AOT_BOT_TOKEN", "AWS_SECRET_ACCESS_KEY"):
        assert key not in env
    assert Path(env["HOME"]).is_relative_to(runtime)
    assert Path(env["XDG_CONFIG_HOME"]).is_relative_to(runtime)
    assert Path(env["XDG_DATA_HOME"]).is_relative_to(runtime)
    assert "DO_NOT_ECHO" not in json.dumps(result)
    shutil.rmtree(package)
    assert subprocess.run([result["executable"], "--help"], capture_output=True, text=True).returncode == 0
    assert runtime.stat().st_mode & 0o777 == 0o700
    assert (runtime / "pyproject.toml").stat().st_mode & 0o777 == 0o600


def test_identical_repeat_reuses_without_uv(tmp_path, package, fake_uv):
    first = bootstrap.bootstrap(package, tmp_path / "runtimes", environ=environment(fake_uv))
    fake_uv.unlink()
    second = bootstrap.bootstrap(package, tmp_path / "runtimes", environ=environment(fake_uv))
    assert second == dict(first, reused=True)


def test_new_source_preserves_old_runtime(tmp_path, package, fake_uv):
    first = bootstrap.bootstrap(package, tmp_path / "runtimes", environ=environment(fake_uv))
    (package / "src" / "agents_over_telegram" / "new.py").write_text("VALUE = 1\n")
    second = bootstrap.bootstrap(package, tmp_path / "runtimes", environ=environment(fake_uv))
    assert first["runtime_dir"] != second["runtime_dir"]
    assert Path(first["executable"]).is_file() and Path(second["executable"]).is_file()


def test_only_allowlisted_files_are_copied(tmp_path, package, fake_uv):
    source = package / "src" / "agents_over_telegram"
    (source / "__pycache__").mkdir()
    (source / "__pycache__" / "cache.py").write_text("PRIVATE")
    (source / "tokens.json").write_text("PRIVATE")
    (package / ".env").write_text("PRIVATE")
    (package / "state.sqlite").write_text("PRIVATE")
    result = bootstrap.bootstrap(package, tmp_path / "runtimes", environ=environment(fake_uv))
    runtime = Path(result["runtime_dir"])
    assert not (runtime / ".env").exists()
    assert not (runtime / "state.sqlite").exists()
    assert not (runtime / "src" / "agents_over_telegram" / "tokens.json").exists()
    assert not (runtime / "src" / "agents_over_telegram" / "__pycache__" / "cache.py").exists()


@pytest.mark.parametrize("relative", ["pyproject.toml", "src/agents_over_telegram/__init__.py"])
def test_source_file_symlink_is_rejected(tmp_path, package, relative):
    target = package / relative
    contents = tmp_path / "elsewhere"
    contents.write_bytes(target.read_bytes())
    target.unlink()
    target.symlink_to(contents)
    with pytest.raises(bootstrap.BootstrapError, match="unsafe"):
        bootstrap.snapshot_source(package)


def test_source_directory_symlink_is_rejected(tmp_path, package):
    (package / "src" / "agents_over_telegram" / "linked").symlink_to(tmp_path, target_is_directory=True)
    with pytest.raises(bootstrap.BootstrapError, match="directory link"):
        bootstrap.snapshot_source(package)


@pytest.mark.parametrize("root_kind", ["symlink", "public", "relative"])
def test_runtime_root_must_be_private(tmp_path, package, root_kind):
    root = tmp_path / "runtimes"
    if root_kind == "symlink":
        actual = tmp_path / "actual"
        actual.mkdir(mode=0o700)
        root.symlink_to(actual, target_is_directory=True)
    elif root_kind == "public":
        root.mkdir(mode=0o755)
    else:
        root = Path("relative-runtime")
    with pytest.raises(bootstrap.BootstrapError):
        bootstrap.bootstrap(package, root, environ={})


def test_explicit_missing_uv_is_actionable_without_fallback(tmp_path, package):
    with pytest.raises(bootstrap.BootstrapError, match="requested uv installer is unavailable"):
        bootstrap.bootstrap(package, tmp_path / "runtimes", installer="uv", environ={"PATH": "/does-not-exist"})
    assert not list((tmp_path / "runtimes").rglob(".ready.json"))


def test_failed_install_can_resume_same_directory(tmp_path, package, fake_uv):
    original = fake_uv.read_text()
    fake_uv.write_text(f"#!{sys.executable}\nraise SystemExit(1)\n")
    with pytest.raises(bootstrap.BootstrapError, match="installation failed"):
        bootstrap.bootstrap(package, tmp_path / "runtimes", environ=environment(fake_uv))
    runtimes = [path for path in (tmp_path / "runtimes").iterdir() if path.is_dir()]
    assert len(runtimes) == 1 and not (runtimes[0] / ".ready.json").exists()
    fake_uv.write_text(original)
    result = bootstrap.bootstrap(package, tmp_path / "runtimes", environ=environment(fake_uv))
    assert result["runtime_dir"] == str(runtimes[0]) and result["reused"] is False


def test_partial_copy_is_replaced_on_resume(tmp_path):
    runtime = tmp_path / "runtime"
    runtime.mkdir(mode=0o700)
    temporary = runtime / "uv.lock.copying"
    temporary.write_bytes(b"partial")
    temporary.chmod(0o600)
    bootstrap.write_snapshot(runtime, {"uv.lock": b"complete"})
    assert (runtime / "uv.lock").read_bytes() == b"complete"
    assert not temporary.exists()


def test_source_mismatch_preserves_existing_runtime(tmp_path, package, fake_uv):
    result = bootstrap.bootstrap(package, tmp_path / "runtimes", environ=environment(fake_uv))
    lock = Path(result["runtime_dir"]) / "uv.lock"
    lock.write_text("changed")
    with pytest.raises(bootstrap.BootstrapError, match="differs"):
        bootstrap.bootstrap(package, tmp_path / "runtimes", environ=environment(fake_uv))
    assert lock.read_text() == "changed" and Path(result["executable"]).exists()


def test_invalid_receipt_is_not_trusted(tmp_path, package, fake_uv):
    result = bootstrap.bootstrap(package, tmp_path / "runtimes", environ=environment(fake_uv))
    (Path(result["runtime_dir"]) / ".ready.json").write_text('{}')
    with pytest.raises(bootstrap.BootstrapError, match="receipt is invalid"):
        bootstrap.bootstrap(package, tmp_path / "runtimes", environ=environment(fake_uv))


def test_symlink_receipt_is_not_read(tmp_path, package, fake_uv):
    result = bootstrap.bootstrap(package, tmp_path / "runtimes", environ=environment(fake_uv))
    receipt = Path(result["runtime_dir"]) / ".ready.json"
    receipt.unlink()
    receipt.symlink_to(tmp_path / "secret")
    with pytest.raises(bootstrap.BootstrapError, match="metadata is unsafe"):
        bootstrap.bootstrap(package, tmp_path / "runtimes", environ=environment(fake_uv))


def test_runtime_validation_failure_does_not_write_receipt(tmp_path, package, fake_uv):
    fake_uv.write_text(fake_uv.read_text().replace('(\"python\", \"0.7.0\")', '(\"python\", \"wrong\")'))
    with pytest.raises(bootstrap.BootstrapError, match="failed validation"):
        bootstrap.bootstrap(package, tmp_path / "runtimes", environ=environment(fake_uv))
    assert not list((tmp_path / "runtimes").rglob(".ready.json"))


def test_install_output_never_appears_in_errors(tmp_path, package, fake_uv):
    fake_uv.write_text(f'#!{sys.executable}\nimport sys\nprint("DO_NOT_ECHO", file=sys.stderr)\nraise SystemExit(1)\n')
    with pytest.raises(bootstrap.BootstrapError) as exc:
        bootstrap.bootstrap(package, tmp_path / "runtimes", offline=True, environ=environment(fake_uv))
    assert "DO_NOT_ECHO" not in str(exc.value)
    assert "without --offline" in str(exc.value)


def test_subprocess_timeout_has_fixed_error(monkeypatch, tmp_path):
    def timeout(*args, **kwargs):
        raise subprocess.TimeoutExpired("SECRET", 1, output="SECRET")
    monkeypatch.setattr(subprocess, "run", timeout)
    with pytest.raises(bootstrap.BootstrapError, match="^Fixed error$"):
        bootstrap.run_checked(["SECRET"], env={}, cwd=tmp_path, timeout=1, error="Fixed error")


def test_install_lock_blocks_other_installer_with_bounded_wait(tmp_path, monkeypatch):
    import fcntl
    root = tmp_path / "runtime"
    root.mkdir(mode=0o700)
    lock = root / ".test.lock"
    fd = os.open(lock, os.O_CREAT | os.O_RDWR, 0o600)
    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    monkeypatch.setattr(bootstrap, "LOCK_TIMEOUT", 0)
    try:
        with pytest.raises(bootstrap.BootstrapError, match="Another runtime install"):
            with bootstrap.install_lock(root, "test"):
                pytest.fail("must not acquire a held lock")
    finally:
        os.close(fd)


def test_lock_link_is_rejected(tmp_path):
    target = tmp_path / "target"
    target.write_text("untouched")
    (tmp_path / ".test.lock").symlink_to(target)
    with pytest.raises(bootstrap.BootstrapError, match="install lock"):
        with bootstrap.install_lock(tmp_path, "test"):
            pytest.fail("must not follow a lock link")
    assert target.read_text() == "untouched"


def test_default_directory_uses_xdg_and_rejects_relative(tmp_path):
    assert bootstrap.default_runtime_root({"XDG_DATA_HOME": str(tmp_path)}) == tmp_path / "agents-over-telegram" / "runtimes"
    with pytest.raises(bootstrap.BootstrapError, match="absolute"):
        bootstrap.default_runtime_root({"XDG_DATA_HOME": "relative"})


def test_old_python_error_precedes_source_reads(tmp_path, monkeypatch):
    monkeypatch.setattr(bootstrap.sys, "version_info", (3, 10, 0))
    with pytest.raises(bootstrap.BootstrapError, match="requires Python 3.11"):
        bootstrap.bootstrap(tmp_path / "missing", tmp_path / "runtimes")
    assert not (tmp_path / "runtimes").exists()


def test_deleted_ready_venv_gives_actionable_replacement_error(tmp_path, package, fake_uv):
    result = bootstrap.bootstrap(package, tmp_path / "runtimes", environ=environment(fake_uv))
    shutil.rmtree(Path(result["runtime_dir"]) / ".venv")
    with pytest.raises(bootstrap.BootstrapError, match="choose a new --runtime-root"):
        bootstrap.bootstrap(package, tmp_path / "runtimes", environ=environment(fake_uv))


def test_ready_venv_symlink_is_not_followed(tmp_path, package, fake_uv):
    result = bootstrap.bootstrap(package, tmp_path / "runtimes", environ=environment(fake_uv))
    venv = Path(result["runtime_dir"]) / ".venv"
    elsewhere = tmp_path / "elsewhere"
    venv.rename(elsewhere)
    venv.symlink_to(elsewhere, target_is_directory=True)
    with pytest.raises(bootstrap.BootstrapError, match="link or non-directory"):
        bootstrap.bootstrap(package, tmp_path / "runtimes", environ=environment(fake_uv))


@pytest.fixture
def fake_pip(monkeypatch):
    calls = []

    def run(command, *, env, cwd, timeout, error):
        calls.append({"command": command, "env": env, "cwd": cwd, "timeout": timeout, "error": error})
        if "venv" in command:
            binary = cwd / ".venv" / "bin"
            binary.mkdir(parents=True, mode=0o700)
            binary.parent.chmod(0o700)
            (binary / "python").write_text("fake Python for mocked subprocess")
        elif "pip" in command and "install" in command and "--no-build-isolation" in command:
            executable = cwd / ".venv" / "bin" / "aot"
            executable.write_text("fake CLI for mocked subprocess")
            executable.chmod(0o700)
        elif "-c" in command:
            return "0.7.0\n"
        elif "--help" in command:
            return "usage: aot setup relay-desktop\n"
        return ""

    monkeypatch.setattr(bootstrap, "run_checked", run)
    return calls


def test_no_uv_selects_python_venv_pip_without_installer_dependency(tmp_path, package, fake_pip):
    result = bootstrap.bootstrap(package, tmp_path / "runtimes", environ={"PATH": "/absent", "PIP_INDEX_URL": "SECRET"})
    assert result["installer"] == "pip" and not result["reused"]
    commands = [call["command"] for call in fake_pip]
    assert commands[0][1:] == ["-I", "-m", "venv", "--without-pip", str(Path(result["runtime_dir"]) / ".venv")]
    assert commands[1][1:] == ["-I", "-m", "ensurepip", "--upgrade", "--default-pip"]
    dependencies, local, check = commands[2:5]
    for flag in ("--isolated", "--no-input", "--require-hashes", "--only-binary=:all:"):
        assert flag in dependencies
    assert dependencies[dependencies.index("--index-url") + 1] == "https://pypi.org/simple"
    assert dependencies.count("-r") == 2
    assert "--no-index" in local and "--no-deps" in local and "--no-build-isolation" in local
    assert check[-1] == "check"
    for call in fake_pip[2:5]:
        assert call["env"]["PIP_CONFIG_FILE"] == os.devnull
        assert "PIP_INDEX_URL" not in call["env"]
        assert "SECRET" not in repr(call)


def test_explicit_pip_does_not_look_for_uv(tmp_path, package, fake_pip, monkeypatch):
    def unexpected(*args, **kwargs):
        pytest.fail("explicit pip must not depend on uv lookup")
    monkeypatch.setattr(bootstrap.shutil, "which", unexpected)
    result = bootstrap.bootstrap(package, tmp_path / "runtimes", installer="pip", environ={})
    assert result["installer"] == "pip"


def test_offline_pip_requires_explicit_wheelhouse_before_subprocess(tmp_path, package, fake_pip):
    with pytest.raises(bootstrap.BootstrapError, match="Offline pip installation requires --wheelhouse"):
        bootstrap.bootstrap(package, tmp_path / "runtimes", installer="pip", offline=True, environ={})
    assert fake_pip == []


def test_offline_pip_never_supplies_an_index_or_build_isolation(tmp_path, package, fake_pip):
    wheelhouse = tmp_path / "wheels"
    wheelhouse.mkdir()
    result = bootstrap.bootstrap(package, tmp_path / "runtimes", installer="pip", offline=True, wheelhouse=wheelhouse, environ={})
    assert result["installer"] == "pip"
    dependencies = fake_pip[2]["command"]
    assert "--no-index" in dependencies and "--index-url" not in dependencies
    assert dependencies[dependencies.index("--find-links") + 1] == str(wheelhouse)
    assert all("https://" not in repr(call["command"]) for call in fake_pip)
    assert "--no-index" in fake_pip[3]["command"]


def test_auto_wheelhouse_selects_pip_even_when_uv_exists(tmp_path, package, fake_pip, fake_uv):
    wheelhouse = tmp_path / "wheels"
    wheelhouse.mkdir()
    result = bootstrap.bootstrap(package, tmp_path / "runtimes", wheelhouse=wheelhouse, environ=environment(fake_uv))
    assert result["installer"] == "pip"
    dependencies = fake_pip[2]["command"]
    assert "--find-links" in dependencies and "--index-url" in dependencies


def test_explicit_uv_wheelhouse_is_rejected(tmp_path, package, fake_pip):
    with pytest.raises(bootstrap.BootstrapError, match="requires --installer pip or auto"):
        bootstrap.bootstrap(package, tmp_path / "runtimes", installer="uv", wheelhouse=tmp_path, environ={})
    assert fake_pip == []


def test_missing_ensurepip_gives_system_package_guidance_without_pip_install(tmp_path, package, monkeypatch):
    calls = []
    def run(command, *, env, cwd, timeout, error):
        calls.append(command)
        if "ensurepip" in command:
            raise bootstrap.BootstrapError(error)
        return ""
    monkeypatch.setattr(bootstrap, "run_checked", run)
    with pytest.raises(bootstrap.BootstrapError, match="python3-venv.*optional uv"):
        bootstrap.bootstrap(package, tmp_path / "runtimes", installer="pip", environ={})
    assert len(calls) == 2 and not any("pip" in command for command in calls)


def test_pip_failure_does_not_emit_subprocess_credentials(tmp_path, package, fake_pip, monkeypatch):
    real_run = bootstrap.run_checked
    def run(command, **kwargs):
        if "pip" in command:
            with pytest.raises(bootstrap.BootstrapError) as caught:
                REAL_RUN_CHECKED(command, **kwargs)
            assert "SECRET" not in str(caught.value)
            raise caught.value
        return real_run(command, **kwargs)
    monkeypatch.setattr(subprocess, "run", lambda *args, **kwargs: subprocess.CompletedProcess(args[0], 1, "SECRET", "SECRET"))
    monkeypatch.setattr(bootstrap, "run_checked", run)
    with pytest.raises(bootstrap.BootstrapError, match="pip runtime installation failed"):
        bootstrap.bootstrap(package, tmp_path / "runtimes", installer="pip", environ={})
    assert not list((tmp_path / "runtimes").rglob(".ready.json"))


def test_pip_runtime_reuse_does_not_need_wheelhouse_or_installers(tmp_path, package, fake_pip, monkeypatch):
    wheelhouse = tmp_path / "wheels"
    wheelhouse.mkdir()
    first = bootstrap.bootstrap(package, tmp_path / "runtimes", offline=True, wheelhouse=wheelhouse, environ={})
    shutil.rmtree(wheelhouse)
    fake_pip.clear()
    monkeypatch.setattr(bootstrap.shutil, "which", lambda *args, **kwargs: pytest.fail("reuse must not check installers"))
    second = bootstrap.bootstrap(package, tmp_path / "runtimes", offline=True, environ={})
    assert second == dict(first, reused=True)
    assert len(fake_pip) == 2  # Version and CLI help only.


def test_requirement_lock_change_gets_new_runtime_address(package):
    before, _ = bootstrap.snapshot_source(package)
    (package / "requirements" / "runtime.txt").write_text("changed locked dependency hashes\n")
    after, _ = bootstrap.snapshot_source(package)
    assert bootstrap.content_address(before) != bootstrap.content_address(after)
    assert "requirements/build.txt" in before and "requirements/runtime.txt" in before


def test_requirements_directory_link_is_rejected(tmp_path, package):
    target = tmp_path / "external-requirements"
    (package / "requirements").rename(target)
    (package / "requirements").symlink_to(target, target_is_directory=True)
    with pytest.raises(bootstrap.BootstrapError, match="requirements directory is unsafe"):
        bootstrap.snapshot_source(package)


@pytest.mark.parametrize("kind", ["relative", "missing", "symlink"])
def test_invalid_wheelhouse_rejected_before_installer(tmp_path, package, fake_pip, kind):
    wheelhouse = tmp_path / "wheels"
    if kind == "relative":
        wheelhouse = Path("relative-wheels")
    elif kind == "symlink":
        wheelhouse.symlink_to(tmp_path, target_is_directory=True)
    with pytest.raises(bootstrap.BootstrapError, match="wheelhouse"):
        bootstrap.bootstrap(package, tmp_path / "runtimes", installer="pip", wheelhouse=wheelhouse, environ={})
    assert fake_pip == []


def test_license_is_snapshotted_and_changes_runtime_address(package):
    before, _ = bootstrap.snapshot_source(package)
    assert before["LICENSE"] == (package / "LICENSE").read_bytes()
    (package / "LICENSE").write_text("Updated license fixture\n")
    after, _ = bootstrap.snapshot_source(package)
    assert bootstrap.content_address(before) != bootstrap.content_address(after)
