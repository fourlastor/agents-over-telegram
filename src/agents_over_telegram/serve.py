"""An always-on collector for one state directory: ``aot serve``.

Owner actions (settings panel taps, approval presses, /settings) must be
answered within seconds, and Telegram updates should keep arriving whether or
not an agent is in the middle of a turn. ``aot serve`` long-polls Telegram
without stopping and does the same work as every other collector: it routes
messages into the inbox, answers buttons and widget requests, and runs the
optional model judge. Agents' own waiters and hooks see its collector lock and
only watch the inbox, so nothing is collected twice.

``aot serve --install`` registers it as a user service (systemd on Linux,
launchd on macOS). The service starts a small launcher that reads
``serve-runtime.json`` in the state directory and runs that AOT executable.
Setup rewrites the pointer whenever it moves a project to a new runtime; the
running collector notices, exits, and the service manager starts it again on
the new runtime.
"""
from __future__ import annotations

import fcntl
import hashlib
import os
import plistlib
import shutil
import subprocess
import sys
import threading
import time
from pathlib import Path

from .credentials import CredentialError, read_private_json, write_private_json

POINTER = 'serve-runtime.json'
LAUNCHER = 'serve_launch.py'
LOG = 'serve.log'
# Held for the whole life of one aot serve, so a state directory never has two.
SERVE_LOCK = 'serve.lock'
# Written by setup --no-serve: no service, and no collector started on demand.
NO_SERVE = 'no-serve'
ON_DEMAND = 'serve-on-demand.json'
# A collector started on demand that died is started again at most this often.
ON_DEMAND_INTERVAL = 60.0
LOG_LIMIT = 1_000_000  # bytes; a bigger log is moved to serve.log.1 when the service starts
POLL_TIMEOUT = 20
BUSY_PAUSE = 2.0
BACKOFF = 2.0
BACKOFF_MAX = 300.0
# A loop iteration takes at most a long poll plus a few requests; one that
# runs past this is stuck, and the collector exits so the service restarts it.
WATCHDOG_LIMIT = 300.0
STUCK_EXIT = 4

# Started by the service manager with the system Python: it only reads the
# pointer and replaces itself with the recorded AOT executable.
LAUNCHER_SOURCE = '''"""Start AOT's collector on the runtime named in serve-runtime.json (written by aot serve --install)."""
import json
import os
import sys

state = os.path.dirname(os.path.abspath(__file__))
with open(os.path.join(state, 'serve-runtime.json'), encoding='utf-8') as stream:
    executable = json.load(stream)['executable']
os.execv(executable, [executable, '--state-dir', state, 'serve'])
'''


def own_executable():
    return Path(sys.executable).absolute().parent / 'aot'


def pointer(state):
    """The executable the service should run, or None when no service is set up."""
    path = Path(state) / POINTER
    if not path.exists() and not path.is_symlink():
        return None
    try:
        value = read_private_json(path)
    except CredentialError:
        return None
    executable = value.get('executable') if isinstance(value, dict) else None
    return Path(executable) if isinstance(executable, str) and os.path.isabs(executable) else None


def follow(state, executable=None):
    """Point an installed service at this runtime; True when the pointer changed.

    Setup calls this, so moving a project to a new runtime also moves the
    collector. Nothing happens when no service is installed.
    """
    current = pointer(state)
    target = Path(executable or own_executable())
    if current is None or os.path.realpath(current) == os.path.realpath(target):
        return False
    write_private_json(Path(state) / POINTER, {'version': 1, 'executable': str(target)}, overwrite=True)
    return True


def moved(state):
    """True when the pointer names another runtime than the one running this collector."""
    current = pointer(state)
    return current is not None and os.path.realpath(current) != os.path.realpath(own_executable())


class Watchdog:
    """Notices a collector loop that stopped making progress.

    The service manager only restarts a process that exits; one that hangs
    (a request that never returns, a lock that is never released) would leave
    every agent deaf. The loop beats once per iteration, and a thread exits
    the process when no beat came for ``limit`` seconds.
    """

    def __init__(self, limit=WATCHDOG_LIMIT, clock=time.monotonic):
        self.limit, self.clock = limit, clock
        self.last = clock()

    def beat(self):
        self.last = self.clock()

    def stuck(self):
        return self.clock() - self.last > self.limit

    def start(self, log, exit_=os._exit, sleep=time.sleep):
        def watch():
            while True:
                sleep(max(1.0, self.limit / 10))
                if self.stuck():
                    log(f'aot serve: no progress for {self.limit:.0f}s; exiting so the service restarts it')
                    exit_(STUCK_EXIT)
                    return
        thread = threading.Thread(target=watch, name='aot-serve-watchdog', daemon=True)
        thread.start()
        return thread


def run(core, state, *, poll_timeout=POLL_TIMEOUT, sleep=time.sleep, keep_running=None, log=None, watchdog=None,
        relay=None):
    """Collect without stopping; return 3 when the runtime moved, 0 when told to stop.

    With ``relay`` it also answers agents' tool calls on the state directory's
    socket (relay_api). Agents run calls themselves when it cannot listen.
    """
    log = log or (lambda text: print(text, flush=True))
    rotate_log(state)
    lock = _hold_serve_lock(state, sleep, keep_running, log)
    if lock is None:
        return 0
    try:
        return _serve(core, state, poll_timeout, sleep, keep_running, log, watchdog, relay)
    finally:
        os.close(lock)


def _hold_serve_lock(state, sleep, keep_running, log):
    """Take this state directory's serve lock, waiting while another aot serve holds it."""
    fd = os.open(Path(state) / SERVE_LOCK, os.O_RDWR | os.O_CREAT | getattr(os, 'O_CLOEXEC', 0), 0o600)
    told = False
    while True:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            return fd
        except OSError:
            pass
        if not told:
            log('aot serve: another aot serve runs for this state directory; waiting until it stops')
            told = True
        if keep_running is not None and not keep_running():
            os.close(fd)
            return None
        sleep(BUSY_PAUSE)


def running(state):
    """True when an aot serve holds this state directory's serve lock."""
    try:
        fd = os.open(Path(state) / SERVE_LOCK, os.O_RDWR | os.O_CREAT | getattr(os, 'O_CLOEXEC', 0), 0o600)
    except OSError:
        return False
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        return True
    else:
        fcntl.flock(fd, fcntl.LOCK_UN)
        return False
    finally:
        os.close(fd)


def start_on_demand(state, *, executable=None, spawn=subprocess.Popen, now=time.time):
    """Start aot serve in the background when nothing else will collect; the reason when not.

    A fallback for machines without the user service: agents call this when
    they start, so owner actions and messages are still handled. It starts
    nothing when the state directory is in fake mode, when setup --no-serve
    opted out, when the service is installed (its manager restarts it), when
    an aot serve already runs, or when one was started less than a minute
    ago (so a collector that dies at once is not restarted in a loop). The
    process is detached from the agent's session, so it outlives it.
    """
    state = Path(state).absolute()
    try:
        mode = read_private_json(state / 'mode.json').get('mode')
    except (CredentialError, OSError):
        mode = None
    if mode != 'live':
        return {'started': False, 'reason': 'not_live'}
    if (state / NO_SERVE).exists():
        return {'started': False, 'reason': 'opted_out'}
    if status(state)['installed']:
        return {'started': False, 'reason': 'service_installed'}
    if running(state):
        return {'started': False, 'reason': 'running'}
    record = state / ON_DEMAND
    try:
        last = read_private_json(record).get('started')
    except (CredentialError, OSError):
        last = None
    if isinstance(last, (int, float)) and 0 <= now() - last < ON_DEMAND_INTERVAL:
        return {'started': False, 'reason': 'recently_started'}
    write_private_json(record, {'version': 1, 'started': now()}, overwrite=record.exists())
    rotate_log(state)
    log = os.open(state / LOG, os.O_WRONLY | os.O_CREAT | os.O_APPEND | getattr(os, 'O_CLOEXEC', 0), 0o600)
    try:
        process = spawn([str(executable or own_executable()), '--state-dir', str(state), 'serve'],
                        stdin=subprocess.DEVNULL, stdout=log, stderr=log, start_new_session=True, close_fds=True)
    finally:
        os.close(log)
    return {'started': True, 'pid': process.pid}


def _serve(core, state, poll_timeout, sleep, keep_running, log, watchdog, relay):
    log(f'aot serve: collecting for {state}')
    if watchdog is not None:
        watchdog.start(log)
    if relay is not None:
        relay.log = log
        try:
            relay.start()
        except Exception as error:
            log(f'aot serve: not answering agents ({type(error).__name__}: {error}); they run their calls themselves')
            relay = None
    try:
        return _loop(core, state, poll_timeout, sleep, keep_running, log, watchdog, relay)
    finally:
        if relay is not None:
            relay.stop()


def _loop(core, state, poll_timeout, sleep, keep_running, log, watchdog, relay=None):
    from .hosts import RUNTIME_MOVED_EXIT
    failures = 0
    while keep_running is None or keep_running():
        if watchdog is not None:
            watchdog.beat()
        if moved(state):
            log('aot serve: a newer runtime is installed; restarting on it')
            return RUNTIME_MOVED_EXIT
        try:
            result = core.collect(timeout=poll_timeout, judge=True)
        except Exception as error:  # the core records intake errors; keep going
            failures += 1
            code = getattr(error, 'code', type(error).__name__)
            pause = min(BACKOFF_MAX, BACKOFF * 2 ** min(failures - 1, 16))
            log(f'aot serve: collection failed ({code}); retrying in {pause:.0f}s')
            sleep(pause)
            continue
        if failures:
            log('aot serve: collecting again')
        failures = 0
        if relay is not None and result.get('updates'):
            # Waiting agents check their inboxes now instead of on their next poll.
            relay.notify()
        if result.get('status') == 'busy':
            # Another collector holds the lock (an older one still stopping).
            sleep(BUSY_PAUSE)
            continue
        try:
            # Sends that waited for a rate limit go out without waiting for an agent.
            core.flush(limit=20)
        except Exception:
            pass
    return 0


def name(state):
    """The service name for one state directory, so several accounts can each have one."""
    digest = hashlib.sha256(os.path.realpath(state).encode()).hexdigest()[:12]
    return f'agents-over-telegram-{digest}'


def _label(state):
    return 'dev.' + name(state).replace('agents-over-telegram-', 'agents-over-telegram.serve.')


def _unit_path(state):
    if sys.platform == 'darwin':
        return Path.home() / 'Library' / 'LaunchAgents' / f'{_label(state)}.plist'
    base = Path(os.environ.get('XDG_CONFIG_HOME') or Path.home() / '.config')
    return base / 'systemd' / 'user' / f'{name(state)}.service'


def _unit(state, python):
    launcher = Path(state) / LAUNCHER
    if sys.platform == 'darwin':
        return plistlib.dumps({'Label': _label(state), 'ProgramArguments': [str(python), str(launcher)],
                               'RunAtLoad': True, 'KeepAlive': True,
                               'StandardOutPath': str(Path(state) / LOG),
                               'StandardErrorPath': str(Path(state) / LOG)}).decode()
    # systemd unit values: quote the paths, and keep the state path out of
    # specifier expansion in the description.
    return (f'[Unit]\nDescription=Agents over Telegram collector ({str(state).replace("%", "%%")})\n'
            'After=network-online.target\n\n'
            f'[Service]\nExecStart="{python}" "{launcher}"\nRestart=always\nRestartSec=5\n\n'
            '[Install]\nWantedBy=default.target\n')


def rotate_log(state):
    """Move a log over LOG_LIMIT to serve.log.1, replacing an older one (launchd only writes it)."""
    log = Path(state) / LOG
    try:
        if log.stat().st_size > LOG_LIMIT:
            os.replace(log, log.with_name(LOG + '.1'))
    except OSError:
        pass


def install(state, *, run_command=subprocess.run, python=None):
    """Write the pointer, the launcher and the user service, then start it."""
    state = Path(state).absolute()
    # Installing it is the opposite of setup --no-serve.
    (state / NO_SERVE).unlink(missing_ok=True)
    python = python or shutil.which('python3') or '/usr/bin/python3'
    write_private_json(state / POINTER, {'version': 1, 'executable': str(own_executable())}, overwrite=True)
    launcher = state / LAUNCHER
    launcher.write_text(LAUNCHER_SOURCE, encoding='utf-8')
    launcher.chmod(0o600)
    unit = _unit_path(state)
    unit.parent.mkdir(parents=True, exist_ok=True)
    unit.write_text(_unit(state, python), encoding='utf-8')
    if sys.platform == 'darwin':
        uid = os.getuid()
        run_command(['launchctl', 'bootout', f'gui/{uid}', str(unit)], check=False, capture_output=True)
        run_command(['launchctl', 'bootstrap', f'gui/{uid}', str(unit)], check=True, capture_output=True)
    else:
        service = f'{name(state)}.service'
        run_command(['systemctl', '--user', 'daemon-reload'], check=True, capture_output=True)
        run_command(['systemctl', '--user', 'enable', '--now', service], check=True, capture_output=True)
        run_command(['systemctl', '--user', 'restart', service], check=True, capture_output=True)
    return {'installed': True, 'service': name(state), 'unit': str(unit), 'executable': str(own_executable()),
            'state_dir': str(state)}


def uninstall(state, *, run_command=subprocess.run):
    """Stop and remove the user service, the launcher and the pointer."""
    state = Path(state).absolute()
    unit = _unit_path(state)
    if sys.platform == 'darwin':
        run_command(['launchctl', 'bootout', f'gui/{os.getuid()}', str(unit)], check=False, capture_output=True)
    else:
        run_command(['systemctl', '--user', 'disable', '--now', f'{name(state)}.service'], check=False,
                    capture_output=True)
    removed = unit.exists()
    for path in (unit, state / LAUNCHER, state / POINTER):
        if path.exists() or path.is_symlink():
            path.unlink()
    if sys.platform != 'darwin':
        run_command(['systemctl', '--user', 'daemon-reload'], check=False, capture_output=True)
    return {'installed': False, 'removed': removed}


def status(state):
    """Whether this state directory's service is set up: its pointer, and a unit that starts its launcher."""
    state = Path(state).absolute()
    current = pointer(state)
    unit = _unit_path(state)
    try:
        serves_this = str(state / LAUNCHER) in unit.read_text(encoding='utf-8')
    except OSError:
        serves_this = False
    return {'installed': current is not None and serves_this, 'service': name(state), 'unit': str(unit),
            'executable': str(current) if current else None}
