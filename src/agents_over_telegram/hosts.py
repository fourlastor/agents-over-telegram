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
import time

from .credentials import CredentialError

RUNTIME_POINTER = '.aot-runtime.json'
MAX_HOOK_INPUT = 1024 * 1024


def runtime_pointer(executable: Path) -> str:
    return json.dumps({'version': 1, 'executable': str(executable)}, indent=2) + '\n'


# A long-running waiter that the plugin launcher started with
# AOT_FOLLOW_RUNTIME=1 exits with this status once the project's runtime
# pointer names another runtime, and the launcher starts it again on that
# one. Without the variable (an older launcher) it keeps running as before.
RUNTIME_MOVED_EXIT = 3


def runtime_moved(project: Path) -> bool:
    """True when following is on and .aot-runtime.json names a runtime other than this one."""
    if os.environ.get('AOT_FOLLOW_RUNTIME') != '1':
        return False
    pointer = read_runtime_pointer(project)
    if pointer is None:
        return False
    own = Path(sys.executable).absolute().parent / 'aot'
    return os.path.realpath(pointer) != os.path.realpath(own)


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
    answer = hook_response(core, client_id, event, payload)
    if answer is not None and args.host == 'claude':
        from .push import with_shell_fallback
        for container, key in ((answer, 'reason'), (answer.get('hookSpecificOutput'), 'additionalContext')):
            if isinstance(container, dict) and isinstance(container.get(key), str):
                container[key] = with_shell_fallback(container[key])
    return answer


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


# Exit status 2 from an ``asyncRewake`` hook wakes Claude Code, even an idle session.
WAKE_EXIT = 2
# Sent when the waiter reaches its time limit with nothing new, so the turn
# that follows ends at once and its Stop hook starts a fresh waiter.
REFRESH_NOTICE = ('[Agents over Telegram] No new Telegram messages. The background listener reached its '
                  'time limit and starts again when this turn ends. Nothing to do: end this turn now, '
                  'without tool calls and with at most one short line.')
# Claude Code stops the hook at its timeout (86400 seconds in claude/hooks.json).
# End a little earlier so the waiter exits on its own terms.
WAKE_DURATION = 82800


def _write_owner(path: Path, token: str) -> None:
    tmp = path.with_name(f'{path.name}.{os.getpid()}.tmp')
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | getattr(os, 'O_CLOEXEC', 0), 0o600)
    with os.fdopen(fd, 'w') as stream:
        stream.write(token)
    os.replace(tmp, path)


def _read_owner(path: Path) -> str | None:
    try:
        return path.read_text()[:64]
    except OSError:
        return None


class _Woken(Exception):
    pass


def _alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except OSError:
        return True
    return True


def run_wake(args) -> int:
    """Wait in the background for new deliveries, then wake the host session.

    Claude Code runs this as an ``asyncRewake`` hook. Plugin monitors do not
    start in every Claude Code session (for example in the desktop app), but
    this hook does. Exit status 2 wakes the session and stderr becomes the
    notice; every other outcome exits 0 without output. One waiter runs per
    client and host session; a newer start takes over from the running one.
    """
    try:
        return _wake(args)
    except Exception:
        return 0


def _keep_alive(core, client_id) -> bool:
    """Whether the owner turned on the daily keep-alive wake for this client."""
    try:
        return bool((core.connection_status(client_id).get('routing') or {}).get('keep_alive'))
    except Exception:
        return False


def _start_collector(state: Path) -> None:
    """Start aot serve on demand when this machine has no service; never fails the caller."""
    from . import serve
    try:
        serve.start_on_demand(state)
    except Exception as error:
        # One line in serve.log, so a start that keeps failing (a sandbox
        # around the agent, say) can be found.
        try:
            with open(Path(state) / serve.LOG, 'a', encoding='utf-8') as log:
                log.write(f'aot: could not start aot serve on demand ({type(error).__name__}: {error})\n')
        except OSError:
            pass


def _wake(args) -> int:
    import fcntl
    import hashlib
    from .cli import LazyTransport, _client, _mode
    from .core import Core
    from .onboarding import read_profile
    from .push import watch

    project = _hook_project(args, {}).resolve()
    profile = read_profile(project)
    if profile is None:
        return 0
    state = Path(profile['state_dir'])
    if not state.is_dir():
        return 0
    fake = profile['mode'] == 'fake'
    _mode(state, fake)
    core = Core(state, LazyTransport(state, fake), recover_sends=False)
    client_id = _client(core, Path(profile['client_file']), profile['client_id'])
    _start_collector(state)
    locks = state / 'wake-locks'
    locks.mkdir(mode=0o700, exist_ok=True)
    key = hashlib.sha256(f'{client_id}\0{args.session or ""}'.encode()).hexdigest()[:24]
    # The newest waiter for a session takes over, so the time limit counts
    # from the latest turn end, not from the first waiter. It records itself
    # as the owner; the running waiter sees that and exits, freeing the lock.
    owner = locks / f'{key}.owner'
    token = f'{os.getpid()}-{time.time_ns()}'
    _write_owner(owner, token)
    fd = os.open(locks / f'{key}.lock', os.O_RDWR | os.O_CREAT | getattr(os, 'O_CLOEXEC', 0), 0o600)
    parent = os.getppid()
    host = getattr(args, 'host_pid', None)

    moved = []

    def keep_running():
        # Re-parenting or a missing host process means the host is gone,
        # so stop instead of polling forever. A newer owner replaces this waiter.
        if not (os.getppid() == parent and (not host or _alive(host)) and _read_owner(owner) == token):
            return False
        # An updated project runtime takes over: the launcher restarts this
        # waiter on it, so collection never runs older code than the pointer.
        if runtime_moved(project):
            moved.append(True)
            return False
        return True

    # The running waiter notices the new owner only at the end of its cycle,
    # which can take longer than a Telegram long poll. Wait without a fixed
    # limit, so the session is never left without a waiter.
    while True:
        if not keep_running():
            os.close(fd)
            return 0
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            break
        except OSError:
            time.sleep(0.2)
    notices = []

    def emit(event):
        if event.get('type') == 'telegram_messages':
            notices.append(event['notice'])
            raise _Woken()

    try:
        from .credentials import read_private_json
        from .relay_api import Waiter
        waiter = Waiter(state, read_private_json(Path(profile['client_file']))['client_secret'])
        result = watch(core, client_id, emit, collect=True, duration=args.duration, keep_running=keep_running,
                       wait_for_change=waiter)
    except _Woken:
        from .push import with_shell_fallback
        # The asyncRewake waiter runs only for Claude Code.
        sys.stderr.write(with_shell_fallback(notices[0]) + '\n')
        sys.stderr.flush()
        return WAKE_EXIT
    if moved:
        return RUNTIME_MOVED_EXIT
    # A new waiter starts only when a turn ends. Without this, a session idle
    # for longer than the time limit would stop hearing Telegram for good.
    # With keep-alive on, the session's current waiter wakes it once when time
    # is up, so the turn end starts a fresh waiter.
    if result.get('reason') == 'duration_elapsed' and _keep_alive(core, client_id) and keep_running():
        sys.stderr.write(REFRESH_NOTICE + '\n')
        sys.stderr.flush()
        return WAKE_EXIT
    # The last check may itself have seen a moved runtime.
    return RUNTIME_MOVED_EXIT if moved else 0
