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

import hashlib
import os
import plistlib
import shutil
import subprocess
import sys
import threading
import time
from pathlib import Path

from . import compat
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
# A collector started on demand stops after this long with no agent talking
# to it (calls and waits), so it is a temporary helper, not a hidden service.
ON_DEMAND_IDLE = 15 * 60.0
# A collector started on demand waits at most this long for another one to
# stop (a newer one replacing an older one), then gives up.
ON_DEMAND_LOCK_WAIT = 60.0
ON_DEMAND_LOCK = 'serve-on-demand.lock'
LOG_LIMIT = 1_000_000  # bytes; a bigger log is moved to serve.log.1 when the service starts
POLL_TIMEOUT = 20
BUSY_PAUSE = 2.0
BACKOFF = 2.0
# Well under WATCHDOG_LIMIT, so a long outage never looks like a stuck loop.
BACKOFF_MAX = 120.0
# A loop iteration takes at most a long poll plus a few requests; one that
# runs past this is stuck, and the collector exits so the service restarts it.
WATCHDOG_LIMIT = 300.0
STUCK_EXIT = 4

# Started by the service manager with the system Python: it only reads the
# pointer and replaces itself with the recorded AOT executable. Windows has no
# exec and Task Scheduler does not restart a task that exits, so there the
# launcher stays and restarts the collector itself, on the runtime the
# pointer names at that moment, until the service is uninstalled.
LAUNCHER_SOURCE = '''"""Start AOT's collector on the runtime named in serve-runtime.json (written by aot serve --install)."""
import json
import os
import sys

state = os.path.dirname(os.path.abspath(__file__))
pointer = os.path.join(state, 'serve-runtime.json')


def executable():
    with open(pointer, encoding='utf-8') as stream:
        return json.load(stream)['executable']


# Tells the collector it runs for the service, so it stops once the service is removed.
os.environ['AOT_SERVE_SUPERVISED'] = '1'
if os.name != 'nt':
    program = executable()
    os.execv(program, [program, '--state-dir', state, 'serve'])

import subprocess
import time

log = os.path.join(state, 'serve.log')
while os.path.exists(pointer):
    try:
        if os.path.getsize(log) > 1000000:
            os.replace(log, log + '.1')
    except OSError:
        pass
    with open(log, 'ab') as output:
        # CREATE_NO_WINDOW: no console window for the collector.
        code = subprocess.call([executable(), '--state-dir', state, 'serve'], stdin=subprocess.DEVNULL,
                               stdout=output, stderr=output, creationflags=0x08000000)
    # 3: a newer runtime is installed; start it at once. Anything else: wait a little.
    time.sleep(1 if code == 3 else 5)
'''


def own_executable():
    return compat.own_aot()


def _task_name(state):
    return '\\' + name(state)


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


def _pointer_value(executable):
    from . import __version__
    return {'version': 1, 'executable': str(executable), 'aot_version': __version__}


def pointer_version(state):
    """The AOT release the pointer names, when the pointer records it."""
    try:
        value = read_private_json(Path(state) / POINTER)
    except (CredentialError, OSError):
        return None
    version = value.get('aot_version') if isinstance(value, dict) else None
    return version if isinstance(version, str) else None


def follow(state, executable=None):
    """Point an installed service at this runtime; True when the pointer changed.

    Setup calls this, so moving a project to a new runtime also moves the
    collector. Nothing happens when no service is installed, and the service
    never moves to an older release than the one it runs: rerunning setup in
    a project that is still on an older runtime leaves it alone.
    """
    from . import __version__
    from .core import _version_key
    current = pointer(state)
    target = Path(executable or own_executable())
    if current is None or os.path.realpath(current) == os.path.realpath(target):
        return False
    recorded, ours = _version_key(pointer_version(state)), _version_key(__version__)
    if recorded is not None and ours is not None and recorded > ours:
        return False
    write_private_json(Path(state) / POINTER, _pointer_value(target), overwrite=True)
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
        relay=None, on_demand=False, clock=time.monotonic):
    """Collect without stopping; return 3 when the runtime moved, 0 when told to stop.

    With ``relay`` it also answers agents' tool calls on the state directory's
    socket (relay_api). Agents run calls themselves when it cannot listen.
    ``on_demand`` is a collector an agent started (start_on_demand): it stops
    when idle, when the service is installed, when the human opts out, or
    when a newer runtime takes over (see _on_demand_done).
    """
    log = log or (lambda text: print(text, flush=True))
    rotate_log(state)
    give_up = clock() + ON_DEMAND_LOCK_WAIT if on_demand else None
    lock = _hold_serve_lock(state, sleep, keep_running, log, give_up=give_up, clock=clock)
    if lock is None:
        return 0
    try:
        done = (lambda: _on_demand_done(state, relay, started, clock)) if on_demand else None
        started = clock()
        return _serve(core, state, poll_timeout, sleep, keep_running, log, watchdog, relay, done)
    finally:
        os.close(lock)


def _hold_serve_lock(state, sleep, keep_running, log, give_up=None, clock=time.monotonic):
    """Take this state directory's serve lock, waiting while another aot serve holds it."""
    fd = os.open(Path(state) / SERVE_LOCK, os.O_RDWR | os.O_CREAT | compat.O_CLOEXEC, 0o600)
    told = False
    while True:
        try:
            compat.lock(fd)
            return fd
        except OSError:
            pass
        if not told:
            log('aot serve: another aot serve runs for this state directory; waiting until it stops')
            told = True
        if (keep_running is not None and not keep_running()) or (give_up is not None and clock() >= give_up):
            os.close(fd)
            return None
        sleep(BUSY_PAUSE)


def opt_out(state):
    """Record that the human wants no collector: no service, and none started on demand."""
    marker = Path(state) / NO_SERVE
    if not marker.exists():
        os.close(os.open(marker, os.O_WRONLY | os.O_CREAT | compat.O_CLOEXEC, 0o600))


def _on_demand_done(state, relay, started, clock):
    """Why a collector started on demand should stop now, or None."""
    state = Path(state)
    if (state / NO_SERVE).exists():
        return 'the human opted out (setup --no-serve)'
    if status(state)['installed']:
        return 'the always-on service is installed'
    recorded = _on_demand_record(state).get('executable')
    if isinstance(recorded, str) and os.path.realpath(recorded) != os.path.realpath(own_executable()):
        return 'a newer runtime takes over'
    last = max(started, getattr(relay, 'last_activity', None) or started)
    if clock() - last >= ON_DEMAND_IDLE:
        return 'no agent used it for a while'
    return None


def _on_demand_record(state):
    try:
        value = read_private_json(Path(state) / ON_DEMAND)
    except (CredentialError, OSError):
        return {}
    return value if isinstance(value, dict) else {}


def running(state):
    """True when an aot serve holds this state directory's serve lock."""
    try:
        fd = os.open(Path(state) / SERVE_LOCK, os.O_RDWR | os.O_CREAT | compat.O_CLOEXEC, 0o600)
    except OSError:
        return False
    try:
        compat.lock(fd)
    except OSError:
        return True
    else:
        compat.unlock(fd)
        return False
    finally:
        os.close(fd)


def start_on_demand(state, *, executable=None, spawn=subprocess.Popen, now=time.time):
    """Start aot serve in the background when nothing else will collect; the reason when not.

    A fallback for machines without the user service: agents call this when
    they start, so owner actions and messages are still handled. It starts
    nothing when the state directory is in fake mode, when setup --no-serve
    opted out, when the service is installed (its manager restarts it), when
    a collector of this runtime or a newer one already runs, or when one was
    started less than a minute ago (so a collector that dies at once is not
    restarted in a loop). An older collector started on demand is replaced:
    the record names this runtime, the older one notices and stops, and the
    new one takes the lock after it.

    The collector is detached from the agent's session and stops by itself
    after ON_DEMAND_IDLE without agents, so it is a temporary helper.
    """
    from . import __version__
    from .core import _version_key
    state = Path(state).absolute()
    executable = Path(executable or own_executable())
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
    # Two agents starting at once must not both spawn one.
    fd = os.open(state / ON_DEMAND_LOCK, os.O_RDWR | os.O_CREAT | compat.O_CLOEXEC, 0o600)
    try:
        try:
            compat.lock(fd)
        except OSError:
            return {'started': False, 'reason': 'starting'}
        record = _on_demand_record(state)
        if running(state):
            theirs, ours = _version_key(record.get('aot_version')), _version_key(__version__)
            same = (isinstance(record.get('executable'), str)
                    and os.path.realpath(record['executable']) == os.path.realpath(executable))
            if same or theirs is None or ours is None or theirs >= ours:
                return {'started': False, 'reason': 'running'}
        else:
            last = record.get('started')
            if isinstance(last, (int, float)) and 0 <= now() - last < ON_DEMAND_INTERVAL:
                return {'started': False, 'reason': 'recently_started'}
        write_private_json(state / ON_DEMAND, {'version': 1, 'started': now(), 'executable': str(executable),
                                               'aot_version': __version__}, overwrite=(state / ON_DEMAND).exists())
        rotate_log(state)
        log = os.open(state / LOG, os.O_WRONLY | os.O_CREAT | os.O_APPEND | compat.O_CLOEXEC, 0o600)
        try:
            # Windows: DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP, so it outlives the agent's console.
            detached = {'creationflags': 0x00000008 | 0x00000200} if compat.WINDOWS else {}
            process = spawn([str(executable), '--state-dir', str(state), 'serve', '--on-demand'],
                            stdin=subprocess.DEVNULL, stdout=log, stderr=log, start_new_session=True,
                            close_fds=True, **detached)
        finally:
            os.close(log)
    finally:
        os.close(fd)
    # Reap it when it exits, so it never lingers as a zombie of a long-lived agent process.
    if hasattr(process, 'wait'):
        threading.Thread(target=process.wait, name='aot-serve-reaper', daemon=True).start()
    return {'started': True, 'pid': process.pid}


def _serve(core, state, poll_timeout, sleep, keep_running, log, watchdog, relay, done=None):
    log(f'aot serve: collecting for {state}' + (' (started on demand)' if done is not None else ''))
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
        return _loop(core, state, poll_timeout, sleep, keep_running, log, watchdog, relay, done)
    finally:
        if relay is not None:
            relay.stop()


def _loop(core, state, poll_timeout, sleep, keep_running, log, watchdog, relay=None, done=None):
    from .hosts import RUNTIME_MOVED_EXIT
    failures = 0
    while keep_running is None or keep_running():
        if watchdog is not None:
            watchdog.beat()
        reason = done() if done is not None else None
        if reason is None and os.environ.get('AOT_SERVE_SUPERVISED') and pointer(state) is None:
            # The service was uninstalled; a service manager that stopped only
            # the launcher (Task Scheduler) must not leave this collector behind.
            reason = 'the always-on service was removed'
        if reason:
            log(f'aot serve: stopping, {reason}')
            return 0
        if moved(state):
            log('aot serve: a newer runtime is installed; restarting on it')
            return RUNTIME_MOVED_EXIT
        try:
            result = core.collect(timeout=poll_timeout, judge=True,
                                  routed=relay.notify if relay is not None else None)
        except Exception as error:  # the core records intake errors; keep going
            failures += 1
            code = getattr(error, 'code', type(error).__name__)
            pause = min(BACKOFF_MAX, BACKOFF * 2 ** min(failures - 1, 16))
            log(f'aot serve: collection failed ({code}); retrying in {pause:.0f}s')
            # Waiting out a failure is progress: beat around the pause.
            if watchdog is not None:
                watchdog.beat()
            sleep(pause)
            if watchdog is not None:
                watchdog.beat()
            continue
        if watchdog is not None:
            watchdog.beat()
        if failures:
            log('aot serve: collecting again')
        failures = 0
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
    if sys.platform == 'win32':
        # The task definition handed to schtasks; Task Scheduler keeps its own copy.
        return Path(state).absolute() / 'serve-task.xml'
    if sys.platform == 'darwin':
        return Path.home() / 'Library' / 'LaunchAgents' / f'{_label(state)}.plist'
    base = Path(os.environ.get('XDG_CONFIG_HOME') or Path.home() / '.config')
    return base / 'systemd' / 'user' / f'{name(state)}.service'


def _unit_encoding():
    # schtasks /XML reads UTF-16.
    return 'utf-16' if sys.platform == 'win32' else 'utf-8'


def _task_xml(state, python):
    from xml.sax.saxutils import escape
    launcher = Path(state) / LAUNCHER
    user = escape(os.environ.get('USERDOMAIN', '') + '\\' + os.environ.get('USERNAME', '')
                  if os.environ.get('USERDOMAIN') else os.environ.get('USERNAME', ''))
    return ('<?xml version="1.0" encoding="UTF-16"?>\n'
            '<Task version="1.2" xmlns="http://schemas.microsoft.com/windows/2004/02/mit/task">\n'
            f'  <RegistrationInfo><Description>Agents over Telegram collector ({escape(str(state))})</Description>'
            '</RegistrationInfo>\n'
            f'  <Triggers><LogonTrigger><Enabled>true</Enabled><UserId>{user}</UserId></LogonTrigger></Triggers>\n'
            f'  <Principals><Principal id="Author"><UserId>{user}</UserId><LogonType>InteractiveToken</LogonType>'
            '<RunLevel>LeastPrivilege</RunLevel></Principal></Principals>\n'
            '  <Settings><MultipleInstancesPolicy>IgnoreNew</MultipleInstancesPolicy>'
            '<DisallowStartIfOnBatteries>false</DisallowStartIfOnBatteries>'
            '<StopIfGoingOnBatteries>false</StopIfGoingOnBatteries>'
            '<ExecutionTimeLimit>PT0S</ExecutionTimeLimit>'
            '<RestartOnFailure><Interval>PT1M</Interval><Count>999</Count></RestartOnFailure>'
            '<StartWhenAvailable>true</StartWhenAvailable><Hidden>true</Hidden></Settings>\n'
            f'  <Actions Context="Author"><Exec><Command>{escape(str(python))}</Command>'
            f'<Arguments>"{escape(str(launcher))}"</Arguments></Exec></Actions>\n'
            '</Task>\n')


def _windows_python():
    """pythonw.exe of the base interpreter: no console window, and not tied to one runtime's venv."""
    base = Path(getattr(sys, '_base_executable', sys.executable))
    for candidate in (base.with_name('pythonw.exe'), Path(shutil.which('pythonw') or '')):
        if candidate.name and candidate.is_file():
            return str(candidate)
    return str(base)


def _unit(state, python):
    launcher = Path(state) / LAUNCHER
    if sys.platform == 'win32':
        return _task_xml(state, python)
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
    if python is None:
        python = _windows_python() if sys.platform == 'win32' else shutil.which('python3') or '/usr/bin/python3'
    write_private_json(state / POINTER, _pointer_value(own_executable()), overwrite=True)
    launcher = state / LAUNCHER
    launcher.write_text(LAUNCHER_SOURCE, encoding='utf-8')
    launcher.chmod(0o600)
    unit = _unit_path(state)
    unit.parent.mkdir(parents=True, exist_ok=True)
    unit.write_text(_unit(state, python), encoding=_unit_encoding())
    if sys.platform == 'win32':
        task = _task_name(state)
        run_command(['schtasks', '/End', '/TN', task], check=False, capture_output=True)
        run_command(['schtasks', '/Create', '/TN', task, '/XML', str(unit), '/F'], check=True, capture_output=True)
        run_command(['schtasks', '/Run', '/TN', task], check=True, capture_output=True)
    elif sys.platform == 'darwin':
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
    """Stop and remove the user service, the launcher and the pointer.

    The files are removed even when the service manager cannot be reached
    (a sandbox refusing launchctl or systemctl), so nothing is left that
    makes the service look installed.
    """
    state = Path(state).absolute()
    unit = _unit_path(state)

    def attempt(argv):
        try:
            run_command(argv, check=False, capture_output=True)
        except (OSError, subprocess.SubprocessError):
            pass
    if sys.platform == 'win32':
        task = _task_name(state)
        attempt(['schtasks', '/End', '/TN', task])
        attempt(['schtasks', '/Delete', '/TN', task, '/F'])
    elif sys.platform == 'darwin':
        attempt(['launchctl', 'bootout', f'gui/{os.getuid()}', str(unit)])
    else:
        attempt(['systemctl', '--user', 'disable', '--now', f'{name(state)}.service'])
    removed = unit.exists()
    for path in (unit, state / LAUNCHER, state / POINTER):
        try:
            if path.exists() or path.is_symlink():
                path.unlink()
        except OSError:
            pass
    if sys.platform not in ('darwin', 'win32'):
        attempt(['systemctl', '--user', 'daemon-reload'])
    return {'installed': False, 'removed': removed}


def status(state):
    """Whether this state directory's service is set up: its pointer, and a unit that starts its launcher."""
    state = Path(state).absolute()
    current = pointer(state)
    unit = _unit_path(state)
    try:
        text = unit.read_text(encoding=_unit_encoding())
        from xml.sax.saxutils import escape
        serves_this = str(state / LAUNCHER) in text or escape(str(state / LAUNCHER)) in text
    except (OSError, UnicodeError):
        serves_this = False
    return {'installed': current is not None and serves_this, 'service': name(state), 'unit': str(unit),
            'executable': str(current) if current else None}
