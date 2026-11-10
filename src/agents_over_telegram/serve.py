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

import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

from .credentials import CredentialError, read_private_json, write_private_json

POINTER = 'serve-runtime.json'
LAUNCHER = 'serve_launch.py'
SERVICE = 'agents-over-telegram'
LAUNCHD_LABEL = 'dev.agents-over-telegram.serve'
POLL_TIMEOUT = 20
BUSY_PAUSE = 2.0
BACKOFF = 2.0
BACKOFF_MAX = 300.0

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


def run(core, state, *, poll_timeout=POLL_TIMEOUT, sleep=time.sleep, keep_running=None, log=None):
    """Collect without stopping; return 3 when the runtime moved, 0 when told to stop."""
    from .hosts import RUNTIME_MOVED_EXIT
    log = log or (lambda text: print(text, flush=True))
    log(f'aot serve: collecting for {state}')
    failures = 0
    while keep_running is None or keep_running():
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


def _unit_paths():
    if sys.platform == 'darwin':
        return Path.home() / 'Library' / 'LaunchAgents' / f'{LAUNCHD_LABEL}.plist'
    base = Path(os.environ.get('XDG_CONFIG_HOME') or Path.home() / '.config')
    return base / 'systemd' / 'user' / f'{SERVICE}.service'


def _unit(state, python):
    launcher = Path(state) / LAUNCHER
    if sys.platform == 'darwin':
        return f'''<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
<key>Label</key><string>{LAUNCHD_LABEL}</string>
<key>ProgramArguments</key><array><string>{python}</string><string>{launcher}</string></array>
<key>RunAtLoad</key><true/><key>KeepAlive</key><true/>
<key>StandardOutPath</key><string>{Path(state) / 'serve.log'}</string>
<key>StandardErrorPath</key><string>{Path(state) / 'serve.log'}</string>
</dict></plist>
'''
    return f'''[Unit]
Description=Agents over Telegram collector ({state})
After=network-online.target

[Service]
ExecStart={python} {launcher}
Restart=always
RestartSec=5

[Install]
WantedBy=default.target
'''


def install(state, *, run_command=subprocess.run, python=None):
    """Write the pointer, the launcher and the user service, then start it."""
    state = Path(state).absolute()
    python = python or shutil.which('python3') or '/usr/bin/python3'
    write_private_json(state / POINTER, {'version': 1, 'executable': str(own_executable())}, overwrite=True)
    launcher = state / LAUNCHER
    launcher.write_text(LAUNCHER_SOURCE, encoding='utf-8')
    launcher.chmod(0o600)
    unit = _unit_paths()
    unit.parent.mkdir(parents=True, exist_ok=True)
    unit.write_text(_unit(state, python), encoding='utf-8')
    if sys.platform == 'darwin':
        uid = os.getuid()
        run_command(['launchctl', 'bootout', f'gui/{uid}', str(unit)], check=False, capture_output=True)
        run_command(['launchctl', 'bootstrap', f'gui/{uid}', str(unit)], check=True, capture_output=True)
    else:
        run_command(['systemctl', '--user', 'daemon-reload'], check=True, capture_output=True)
        run_command(['systemctl', '--user', 'enable', '--now', f'{SERVICE}.service'], check=True, capture_output=True)
        run_command(['systemctl', '--user', 'restart', f'{SERVICE}.service'], check=True, capture_output=True)
    return {'installed': True, 'unit': str(unit), 'executable': str(own_executable()), 'state_dir': str(state)}


def uninstall(state, *, run_command=subprocess.run):
    """Stop and remove the user service, the launcher and the pointer."""
    state = Path(state).absolute()
    unit = _unit_paths()
    if sys.platform == 'darwin':
        run_command(['launchctl', 'bootout', f'gui/{os.getuid()}', str(unit)], check=False, capture_output=True)
    else:
        run_command(['systemctl', '--user', 'disable', '--now', f'{SERVICE}.service'], check=False, capture_output=True)
    removed = unit.exists()
    for path in (unit, state / LAUNCHER, state / POINTER):
        if path.exists() or path.is_symlink():
            path.unlink()
    if sys.platform != 'darwin':
        run_command(['systemctl', '--user', 'daemon-reload'], check=False, capture_output=True)
    return {'installed': False, 'removed': removed}


def status(state):
    current = pointer(state)
    unit = _unit_paths()
    return {'installed': current is not None and unit.exists(), 'unit': str(unit),
            'executable': str(current) if current else None}
