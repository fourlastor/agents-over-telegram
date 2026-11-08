"""Whether Codex still trusts AOT's plugin hooks.

Codex runs a plugin hook only while two hashes match what the human approved
with ``/hooks``: the hook declaration (``trusted_hash``) and a SHA-256 of the
whole plugin folder (``trusted_content_hash``). Every AOT release changes the
plugin folder, so after each Codex plugin update Codex stops running the hooks
until the human approves them again. Codex does not say so to the agent.

This module recomputes the folder hash exactly as Codex does
(``codex-rs/hooks/src/engine/plugin_fingerprint.rs``) and compares it with the
approval stored in Codex's ``config.toml``. It only reads files.
"""
from __future__ import annotations

import hashlib
import os
import stat
import struct
import tomllib
from pathlib import Path

PLUGIN_ID = 'agents-over-telegram@agents-over-telegram'
HOOKS_FILE = 'codex/hooks.json'
EVENTS = ('session_start', 'user_prompt_submit', 'post_tool_use', 'stop')
_SKIPPED_NAMES = ('__pycache__', '.venv')
_SKIPPED_SUFFIXES = ('.pyc', '.egg-info')

APPROVE_HINT = ("Codex is not running AOT's plugin hooks: they changed with the last plugin update "
                "and need approval again. Ask the user to approve them in the ChatGPT desktop app, "
                "or with /hooks in the Codex CLI in this project. Until then, Telegram messages reach this chat only when "
                "it is idle.")


def plugin_content_hash(root: Path) -> str:
    """The SHA-256 Codex stores as ``trusted_content_hash`` for a plugin folder."""
    root = Path(root)
    if not stat.S_ISDIR(os.lstat(root).st_mode):
        raise OSError('plugin root is not a directory')
    digest = hashlib.sha256(b'codex-plugin-bundle-v1')
    pending = [root]
    while pending:
        path = pending.pop()
        relative = path.relative_to(root)
        name = relative.name
        if name and (name in _SKIPPED_NAMES or name.endswith(_SKIPPED_SUFFIXES)):
            continue
        encoded = os.fsencode(str(relative)) if name else b''
        digest.update(struct.pack('<Q', len(encoded)))
        digest.update(encoded)
        info = os.lstat(path)
        if stat.S_ISDIR(info.st_mode):
            digest.update(b'directory')
            entries = sorted(os.listdir(path), key=os.fsencode)
            pending.extend(path / entry for entry in reversed(entries))
        elif stat.S_ISREG(info.st_mode):
            digest.update(b'file')
            digest.update(struct.pack('<Q', info.st_size))
            with open(path, 'rb') as handle:
                read = 0
                while chunk := handle.read(65536):
                    read += len(chunk)
                    digest.update(chunk)
            if read != info.st_size:
                raise OSError('plugin file changed while fingerprinting')
        else:
            raise OSError('unsupported plugin entry type')
    return 'sha256:' + digest.hexdigest()


def codex_home() -> Path:
    value = os.environ.get('CODEX_HOME')
    return Path(value) if value else Path.home() / '.codex'


def _version_key(path: Path):
    return tuple(int(part) if part.isdigit() else 0 for part in path.name.split('.'))


def installed_plugin_root(home: Path) -> Path | None:
    """The installed AOT plugin folder in Codex's cache, newest version first."""
    marketplace, plugin = PLUGIN_ID.split('@')[1], PLUGIN_ID.split('@')[0]
    base = home / 'plugins' / 'cache' / marketplace / plugin
    try:
        versions = [entry for entry in base.iterdir()
                    if entry.is_dir() and (entry / '.codex-plugin' / 'plugin.json').is_file()]
    except OSError:
        return None
    return max(versions, key=_version_key) if versions else None


def status(home: Path | None = None) -> dict:
    """``approved``, ``needs_approval``, ``not_installed`` or ``unknown``, with a reason."""
    home = Path(home) if home is not None else codex_home()
    root = installed_plugin_root(home)
    if root is None:
        return {'status': 'not_installed'}
    result = {'plugin_version': root.name}
    try:
        config = tomllib.loads((home / 'config.toml').read_text())
    except (OSError, ValueError):
        return {**result, 'status': 'unknown', 'reason': 'codex_config_unreadable'}
    if not config.get('plugins', {}).get(PLUGIN_ID, {}).get('enabled', False):
        return {**result, 'status': 'not_installed', 'reason': 'plugin_disabled'}
    try:
        current = plugin_content_hash(root)
    except OSError:
        return {**result, 'status': 'unknown', 'reason': 'plugin_unreadable'}
    states = config.get('hooks', {}).get('state', {})
    for event in EVENTS:
        entry = states.get(f'{PLUGIN_ID}:{HOOKS_FILE}:{event}:0:0', {})
        if not entry.get('trusted_hash'):
            return {**result, 'status': 'needs_approval', 'reason': 'hook_never_approved'}
        trusted = entry.get('trusted_content_hash')
        if trusted is None:
            # An older approval has no content hash. Codex compares the folder
            # with a baseline file it pins on first use; with no file yet, it
            # pins the current folder, so the hooks still run.
            trusted = _legacy_baseline(home)
        if trusted is not None and trusted != current:
            return {**result, 'status': 'needs_approval', 'reason': 'plugin_changed_since_approval'}
    return {**result, 'status': 'approved'}


def _legacy_baseline(home: Path) -> str | None:
    """The legacy approval baseline Codex keeps in ``.hook-trust`` (``plugin_trust.rs``)."""
    digest = hashlib.sha256()
    for part in (os.fsencode(str(home / 'config.toml')), PLUGIN_ID.encode()):
        digest.update(struct.pack('<Q', len(part)))
        digest.update(part)
    try:
        return (home / '.hook-trust' / digest.hexdigest()).read_text().strip() or None
    except OSError:
        return None


def codex_project_uses(client_file: Path, project: Path | None = None) -> bool:
    """True when this project's Codex configuration runs AOT with this client file."""
    start = Path(project) if project is not None else Path.cwd()
    wanted = str(Path(client_file).absolute())
    # Codex starts the server in the session folder, which may be below the project.
    for folder in (start, *list(start.parents)[:4]):
        try:
            config = tomllib.loads((folder / '.codex' / 'config.toml').read_text())
        except (OSError, ValueError):
            continue
        args = config.get('mcp_servers', {}).get('telegram', {}).get('args', [])
        if isinstance(args, list) and wanted in [str(arg) for arg in args]:
            return True
    return False
