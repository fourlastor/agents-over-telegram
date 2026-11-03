"""Host integration helpers: hook answers and the per-project runtime pointer.

Host plugins (Claude Code, Pi, Amp) are installed once but must start the
runtime chosen for each project. Setup records that runtime in a small,
non-secret ``.aot-runtime.json`` next to the project profile. Plugin launchers
read only that exact project directory; they never search parent directories.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
import sys

from .credentials import CredentialError

RUNTIME_POINTER = '.aot-runtime.json'
MAX_HOOK_INPUT = 1024 * 1024


def runtime_pointer(executable: Path) -> str:
    return json.dumps({'version': 1, 'executable': str(executable)}, indent=2) + '\n'


def read_runtime_pointer(project: Path) -> Path | None:
    """Return the recorded executable, or None if the pointer is absent or unsafe."""
    from .onboarding import _read_local
    path = Path(project) / RUNTIME_POINTER
    if not path.exists() and not path.is_symlink():
        return None
    try:
        # Same rule as the plugin launcher: owned, single link, not group or world writable.
        if path.lstat().st_mode & 0o022:
            return None
        value = json.loads(_read_local(path))
        if not isinstance(value, dict) or set(value) != {'version', 'executable'}:
            return None
        executable = value['executable']
        if (type(value['version']) is not int or value['version'] != 1 or not isinstance(executable, str)
                or '\x00' in executable or not Path(executable).is_absolute()):
            return None
        return Path(executable)
    except (CredentialError, OSError, ValueError, TypeError, KeyError):
        return None


class _NoTransport:
    """Hooks never contact Telegram; any attempt fails closed."""

    def __getattr__(self, name):
        raise CredentialError('hook_transport_refused')


def _hook_project(args, payload: dict) -> Path:
    if args.project_dir is not None:
        return Path(args.project_dir)
    if args.host == 'claude' and os.environ.get('CLAUDE_PROJECT_DIR'):
        return Path(os.environ['CLAUDE_PROJECT_DIR'])
    cwd = payload.get('cwd')
    if isinstance(cwd, str) and cwd and '\x00' not in cwd:
        return Path(cwd)
    return Path.cwd()


def hook_answer(args, raw: str) -> dict | None:
    from .cli import _client
    from .core import Core
    from .onboarding import read_profile
    from .push import hook_response

    payload = json.loads(raw) if raw.strip() else {}
    if not isinstance(payload, dict):
        return None
    event = payload.get('hook_event_name')
    if not isinstance(event, str):
        return None
    profile = read_profile(_hook_project(args, payload).resolve())
    if profile is None:
        return None
    state = Path(profile['state_dir'])
    if not state.is_dir():
        return None
    core = Core(state, _NoTransport(), recover_sends=False)
    client_id = _client(core, Path(profile['client_file']), profile['client_id'])
    _maybe_collect(core, state, profile['mode'] == 'fake', getattr(args, 'collect_interval', 0))
    return hook_response(core, client_id, event, payload)


def _maybe_collect(core, state: Path, fake: bool, interval) -> None:
    """Collect once if nobody tried recently. Hosts without a background watcher
    (Codex) use this so a busy turn still sees new messages. The shared
    collector lock keeps this from competing with a running listener."""
    if not isinstance(interval, (int, float)) or isinstance(interval, bool) or interval <= 0:
        return
    with core._db() as db:
        row = db.execute('SELECT last_attempt FROM account WHERE singleton=1').fetchone()
    if row is None or (row['last_attempt'] is not None and core.clock() - row['last_attempt'] < interval):
        return
    from .cli import LazyTransport, _mode
    try:
        _mode(state, fake)
        core.transport = LazyTransport(state, fake)
        core.collect(timeout=0)
    except Exception:
        # Intake errors are recorded by the core; the hook still answers.
        pass
    finally:
        core.transport = _NoTransport()


def run_hook(args) -> dict | None:
    """Answer one hook event. Any failure produces no output and exit status 0,
    so a broken or unconfigured project never blocks the host."""
    try:
        raw = sys.stdin.read(MAX_HOOK_INPUT + 1)
        if len(raw) > MAX_HOOK_INPUT:
            return None
        return hook_answer(args, raw)
    except Exception:
        return None
