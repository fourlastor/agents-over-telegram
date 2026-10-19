#!/usr/bin/env python3
"""Prepare a private, stable runtime from an installed plugin; never configure Telegram."""
from __future__ import annotations

import argparse
from contextlib import contextmanager
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import stat
import subprocess
import sys
import time
from typing import Mapping

FORMAT_VERSION = 2
INSTALL_TIMEOUT = 300
VALIDATE_TIMEOUT = 20
LOCK_TIMEOUT = 30
PACKAGE = "agents_over_telegram"
MAX_SOURCE_BYTES = 20 * 1024 * 1024


class BootstrapError(ValueError):
    """An actionable diagnostic containing no subprocess output or credentials."""


def isolated_environment(environ: Mapping[str, str]) -> dict[str, str]:
    """Do not let an active environment or uv settings redirect the installation."""
    # Bootstrap needs no app, cloud, Telegram, proxy-auth or package-index secrets.
    allowed = {"PATH", "LANG", "LC_ALL", "LC_CTYPE", "TZ", "SYSTEMROOT"}
    env = {key: value for key, value in environ.items() if key in allowed}
    env["PYTHONNOUSERSITE"] = "1"
    return env


def installation_environment(runtime: Path, environ: Mapping[str, str]) -> tuple[dict[str, str], Path]:
    env = isolated_environment(environ)
    cache_home = Path(environ.get("XDG_CACHE_HOME") or str(Path.home() / ".cache"))
    if not cache_home.is_absolute():
        raise BootstrapError("XDG_CACHE_HOME must be absolute or unset before runtime setup.")
    for name, key in ((".home", "HOME"), (".config", "XDG_CONFIG_HOME"), (".data", "XDG_DATA_HOME"), (".cache", "XDG_CACHE_HOME")):
        path = runtime / name
        private_directory(path)
        env[key] = str(path)
    # Only the dependency cache is shared. User uv config, netrc and credentials
    # remain outside the isolated home/config/data directories.
    return env, cache_home / "uv"


def default_runtime_root(environ: Mapping[str, str]) -> Path:
    data_home = environ.get("XDG_DATA_HOME")
    if data_home:
        base = Path(data_home)
        if not base.is_absolute():
            raise BootstrapError("XDG_DATA_HOME must be absolute, or pass --runtime-root with an absolute path.")
    else:
        base = Path.home() / ".local" / "share"
    return base / "agents-over-telegram" / "runtimes"


def regular_bytes(path: Path) -> bytes:
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
        with os.fdopen(fd, "rb") as stream:
            info = os.fstat(stream.fileno())
            if not stat.S_ISREG(info.st_mode) or info.st_size > MAX_SOURCE_BYTES:
                raise BootstrapError("A packaged runtime file is invalid or too large; reinstall the plugin.")
            return stream.read(MAX_SOURCE_BYTES + 1)
    except OSError:
        raise BootstrapError("A packaged runtime file is missing or unsafe; reinstall the plugin.") from None


def snapshot_source(plugin_root: Path) -> tuple[dict[str, bytes], str]:
    """The allowlist deliberately excludes configuration, credentials, caches and tests."""
    import tomllib

    if (plugin_root / "requirements").is_symlink():
        raise BootstrapError("The packaged requirements directory is unsafe; reinstall the plugin.")
    files = {name: regular_bytes(plugin_root / name) for name in ("pyproject.toml", "uv.lock", "requirements/runtime.txt", "requirements/build.txt")}
    package_root = plugin_root / "src" / PACKAGE
    if (plugin_root / "src").is_symlink() or package_root.is_symlink() or not package_root.is_dir():
        raise BootstrapError("The packaged source directory is missing or unsafe; reinstall the plugin.")
    for directory, directories, names in os.walk(package_root, followlinks=False):
        base = Path(directory)
        if any((base / name).is_symlink() for name in directories):
            raise BootstrapError("The packaged source contains a directory link; reinstall the plugin.")
        directories[:] = sorted(name for name in directories if name != "__pycache__")
        for name in sorted(names):
            if name.endswith(".py"):
                path = base / name
                files[path.relative_to(plugin_root).as_posix()] = regular_bytes(path)
    if f"src/{PACKAGE}/__init__.py" not in files or sum(map(len, files.values())) > MAX_SOURCE_BYTES:
        raise BootstrapError("The packaged runtime source is incomplete or too large; reinstall the plugin.")
    try:
        project = tomllib.loads(files["pyproject.toml"].decode("utf-8"))["project"]
        version = project["version"]
        if project["name"] != "agents-over-telegram" or not isinstance(version, str) or not re.fullmatch(r"[A-Za-z0-9.+-]{1,64}", version):
            raise ValueError
    except (ValueError, KeyError, TypeError, UnicodeError):
        raise BootstrapError("The packaged project metadata is invalid; reinstall the plugin.") from None
    return files, version


def content_address(files: dict[str, bytes]) -> str:
    digest = hashlib.sha256()
    identity = {"format": FORMAT_VERSION, "python": str(Path(sys.executable).resolve()),
                "version": sys.version, "cache_tag": sys.implementation.cache_tag}
    digest.update(json.dumps(identity, sort_keys=True).encode())
    for name, contents in sorted(files.items()):
        digest.update(b"\0" + name.encode() + b"\0" + hashlib.sha256(contents).digest())
    return digest.hexdigest()


def private_directory(path: Path) -> None:
    """Reject symlink traversal; only the private subtree must be owned and mode 0700."""
    if not path.is_absolute() or ".." in path.parts:
        raise BootstrapError("The runtime directory must be an absolute path without '..'.")
    try:
        current = Path(path.anchor)
        for part in path.parts[1:]:
            current /= part
            try:
                current.mkdir(mode=0o700)
            except FileExistsError:
                pass
            info = current.lstat()
            if not stat.S_ISDIR(info.st_mode):
                raise BootstrapError("The runtime directory contains a link or non-directory; choose a private directory.")
            if info.st_mode & 0o022 and not info.st_mode & stat.S_ISVTX:
                raise BootstrapError("A runtime parent directory is writable by other users; choose a private directory.")
        info = path.lstat()
        if info.st_uid != os.getuid() or info.st_mode & 0o077:
            raise BootstrapError("The runtime directory must be owned by you and have permissions 0700.")
    except OSError:
        raise BootstrapError("Cannot create the private runtime directory; check its location and permissions.") from None


def checked_private_file(path: Path) -> bytes:
    try:
        info = path.lstat()
        if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_nlink != 1 or info.st_mode & 0o077:
            raise BootstrapError("The saved runtime metadata is unsafe; choose a new private runtime directory.")
        return regular_bytes(path)
    except OSError:
        raise BootstrapError("Cannot read the saved runtime metadata; choose a new private runtime directory.") from None


@contextmanager
def install_lock(root: Path, address: str):
    import fcntl

    try:
        fd = os.open(root / f".{address}.lock", os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    except OSError:
        raise BootstrapError("Cannot open the runtime install lock; check the private runtime directory.") from None
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_nlink != 1 or info.st_mode & 0o077:
            raise BootstrapError("The runtime install lock is unsafe; choose a new private runtime directory.")
        deadline = time.monotonic() + LOCK_TIMEOUT
        while True:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                if time.monotonic() >= deadline:
                    raise BootstrapError("Another runtime install is still running; wait for it to finish and rerun.") from None
                time.sleep(0.1)
        yield
    finally:
        os.close(fd)


def write_snapshot(runtime: Path, files: dict[str, bytes]) -> None:
    for name, contents in sorted(files.items()):
        destination = runtime / name
        private_directory(destination.parent)
        if destination.exists() or destination.is_symlink():
            if checked_private_file(destination) != contents:
                raise BootstrapError("An existing runtime source differs from this package; choose a new --runtime-root. Existing runtimes were preserved.")
            continue
        # A killed write is detected on rerun instead of trusting a partial source.
        temporary = destination.with_name(destination.name + ".copying")
        if temporary.exists() or temporary.is_symlink():
            checked_private_file(temporary)
            temporary.unlink()
        try:
            fd = os.open(temporary, os.O_CREAT | os.O_EXCL | os.O_WRONLY | os.O_NOFOLLOW, 0o600)
            with os.fdopen(fd, "wb") as stream:
                stream.write(contents)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, destination)
        except OSError:
            raise BootstrapError("Cannot copy the runtime source; check free space and rerun.") from None


def run_checked(command: list[str], *, env: dict[str, str], cwd: Path, timeout: int, error: str) -> str:
    try:
        result = subprocess.run(command, env=env, cwd=cwd, capture_output=True, text=True, timeout=timeout)
    except (OSError, UnicodeError, subprocess.TimeoutExpired):
        raise BootstrapError(error) from None
    if result.returncode:
        raise BootstrapError(error)
    return result.stdout


def validate_runtime(runtime: Path, version: str, env: dict[str, str]) -> Path:
    private_directory(runtime / ".venv")
    private_directory(runtime / ".venv" / "bin")
    executable = runtime / ".venv" / "bin" / "aot"
    python = runtime / ".venv" / "bin" / "python"
    try:
        info = executable.lstat()
        if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or not os.access(executable, os.X_OK):
            raise OSError
    except OSError:
        raise BootstrapError("The prepared runtime executable is unavailable; choose a new --runtime-root to prepare a replacement. Existing runtimes were preserved.") from None
    error = "The prepared runtime failed validation; choose a new --runtime-root to prepare a replacement. Existing runtimes were preserved."
    actual_version = run_checked([str(python), "-I", "-c", f"import {PACKAGE}; print({PACKAGE}.__version__)"],
                                 env=env, cwd=runtime, timeout=VALIDATE_TIMEOUT, error=error).strip()
    if actual_version != version:
        raise BootstrapError(error)
    help_text = run_checked([str(executable), "--help"], env=env, cwd=runtime, timeout=VALIDATE_TIMEOUT, error=error)
    if "setup" not in help_text or "relay-desktop" not in help_text:
        raise BootstrapError(error)
    return executable


def validate_wheelhouse(path: Path | None) -> Path | None:
    if path is None:
        return None
    if not path.is_absolute() or ".." in path.parts:
        raise BootstrapError("--wheelhouse must be an absolute local directory.")
    try:
        if not stat.S_ISDIR(path.lstat().st_mode) or not os.access(path, os.R_OK | os.X_OK):
            raise OSError
    except OSError:
        raise BootstrapError("--wheelhouse must name a readable local directory, not a symlink.") from None
    return path


def install_with_uv(runtime: Path, uv: str, cache: Path, env: dict[str, str], *, offline: bool) -> None:
    command = [str(Path(uv).absolute()), "--no-config", "--cache-dir", str(cache), "sync", "--project", str(runtime),
               "--python", str(Path(sys.executable).resolve()), "--no-managed-python", "--no-python-downloads",
               "--frozen", "--no-dev", "--no-default-groups", "--no-editable", "--keyring-provider", "disabled"]
    if offline:
        command.append("--offline")
    error = ("Offline uv installation failed or timed out. Ensure the locked dependencies are cached, or rerun without --offline."
             if offline else "uv runtime installation failed or timed out. Check uv, network access and free space, then rerun. Existing runtimes were preserved.")
    run_checked(command, env=env, cwd=runtime, timeout=INSTALL_TIMEOUT, error=error)


def install_with_pip(runtime: Path, env: dict[str, str], *, offline: bool, wheelhouse: Path | None) -> None:
    if offline and wheelhouse is None:
        raise BootstrapError("Offline pip installation requires --wheelhouse with the locked runtime and build wheels; uv's cache is not a pip wheelhouse.")
    python = str(Path(sys.executable).resolve())
    venv = runtime / ".venv"
    prerequisite_error = ("Python's venv/ensurepip support is unavailable. Use Python 3.11+ with venv and ensurepip "
                          "(on Debian/Ubuntu, install the matching python3-venv package), or install optional uv and rerun.")
    run_checked([python, "-I", "-m", "venv", "--without-pip", str(venv)],
                env=env, cwd=runtime, timeout=INSTALL_TIMEOUT, error=prerequisite_error)
    private_directory(venv)
    private_directory(venv / "bin")
    venv_python = str(venv / "bin" / "python")
    run_checked([venv_python, "-I", "-m", "ensurepip", "--upgrade", "--default-pip"],
                env=env, cwd=runtime, timeout=INSTALL_TIMEOUT, error=prerequisite_error)
    # --isolated alone still loads global/site pip configuration. os.devnull
    # disables all config files, while the private HOME excludes netrc credentials.
    pip_env = dict(env, PIP_CONFIG_FILE=os.devnull)
    pip = [venv_python, "-I", "-m", "pip", "--isolated", "--disable-pip-version-check", "--no-input", "--no-cache-dir"]
    sources = ["--no-index"] if offline else ["--index-url", "https://pypi.org/simple"]
    if wheelhouse is not None:
        sources += ["--find-links", str(wheelhouse)]
    error = ("Offline pip installation failed or timed out. Check that --wheelhouse contains the locked wheels for this Python and platform, then rerun."
             if offline else "pip runtime installation failed or timed out. Check network access, compatible wheels and free space, then rerun. Existing runtimes were preserved.")
    run_checked([*pip, "install", *sources, "--require-hashes", "--only-binary=:all:",
                 "-r", str(runtime / "requirements" / "runtime.txt"), "-r", str(runtime / "requirements" / "build.txt")],
                env=pip_env, cwd=runtime, timeout=INSTALL_TIMEOUT, error=error)
    # The local source is already content-addressed. Its build dependencies were
    # installed with hashes above; neither build isolation nor dependency fetches
    # are allowed while constructing this project's wheel.
    run_checked([*pip, "install", "--no-index", "--no-deps", "--no-build-isolation", str(runtime)],
                env=pip_env, cwd=runtime, timeout=INSTALL_TIMEOUT, error=error)
    run_checked([*pip, "check"], env=pip_env, cwd=runtime, timeout=VALIDATE_TIMEOUT,
                error="The installed runtime dependency check failed; verify the packaged locks or choose a new --runtime-root.")


def bootstrap(plugin_root: Path, runtime_root: Path, *, offline: bool = False, installer: str = "auto",
              wheelhouse: Path | None = None, environ: Mapping[str, str] | None = None) -> dict[str, object]:
    if sys.version_info < (3, 11):
        raise BootstrapError("Runtime setup requires Python 3.11 or newer. Rerun this script with a compatible Python interpreter.")
    if os.name != "posix":
        raise BootstrapError("This runtime bootstrap currently supports POSIX systems; use a Linux or macOS Python interpreter.")
    if installer not in {"auto", "uv", "pip"}:
        raise BootstrapError("Choose --installer auto, uv or pip.")
    if installer == "uv" and wheelhouse is not None:
        raise BootstrapError("--wheelhouse requires --installer pip or auto; uv uses its own dependency cache.")
    environ = os.environ if environ is None else environ
    files, version = snapshot_source(plugin_root)
    address = content_address(files)
    private_directory(runtime_root)
    runtime = runtime_root / address
    with install_lock(runtime_root, address):
        private_directory(runtime)
        write_snapshot(runtime, files)
        env, cache = installation_environment(runtime, environ)
        receipt = runtime / ".ready.json"
        expected = {"format": FORMAT_VERSION, "address": address, "version": version}
        reused = False
        if receipt.exists() or receipt.is_symlink():
            receipt_bytes = checked_private_file(receipt)
            try:
                saved = json.loads(receipt_bytes)
                selected = saved["installer"]
                if selected not in {"uv", "pip"} or saved != dict(expected, installer=selected):
                    raise ValueError
            except (ValueError, KeyError, TypeError, UnicodeError):
                raise BootstrapError("The saved runtime receipt is invalid; choose a new --runtime-root. Existing runtimes were preserved.") from None
            reused = True
        if not reused:
            wheelhouse = validate_wheelhouse(wheelhouse)
            uv = shutil.which("uv", path=env.get("PATH", os.defpath)) if installer != "pip" else None
            selected = ("pip" if wheelhouse is not None else "uv" if uv is not None else "pip") if installer == "auto" else installer
            if selected == "uv" and uv is None:
                raise BootstrapError("The requested uv installer is unavailable. Rerun with --installer auto or pip, or install optional uv.")
            venv = runtime / ".venv"
            if venv.exists() or venv.is_symlink():
                private_directory(venv)
            previous_umask = os.umask(0o077)
            try:
                if selected == "uv":
                    install_with_uv(runtime, uv, cache, env, offline=offline)
                else:
                    install_with_pip(runtime, env, offline=offline, wheelhouse=wheelhouse)
            finally:
                os.umask(previous_umask)
        executable = validate_runtime(runtime, version, env)
        if not reused:
            write_snapshot(runtime, {".ready.json": (json.dumps(dict(expected, installer=selected), sort_keys=True) + "\n").encode()})
        return {"status": "ready", "version": version, "executable": str(executable),
                "runtime_dir": str(runtime), "reused": reused, "installer": selected}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--runtime-root", type=Path, help="Absolute private runtime directory (default: XDG data home)")
    parser.add_argument("--offline", action="store_true", help="Use uv cache or an explicit pip --wheelhouse without networking")
    parser.add_argument("--installer", choices=("auto", "uv", "pip"), default="auto", help="Prefer uv when available, otherwise Python venv/pip (default: auto)")
    parser.add_argument("--wheelhouse", type=Path, help="Absolute directory of locked wheels; selects pip in auto mode and is required for offline pip")
    args = parser.parse_args()
    try:
        if sys.version_info < (3, 11):
            raise BootstrapError("Runtime setup requires Python 3.11 or newer. Rerun this script with a compatible Python interpreter.")
        result = bootstrap(Path(__file__).resolve().parents[1], args.runtime_root or default_runtime_root(os.environ), offline=args.offline, installer=args.installer, wheelhouse=args.wheelhouse)
    except BootstrapError as exc:
        print(json.dumps({"status": "error", "message": str(exc)}), file=sys.stderr)
        return 2
    print(json.dumps(result, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
