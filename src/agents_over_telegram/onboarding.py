"""Local operator onboarding and exact-directory, non-secret profile references."""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import re
import shlex
import stat
import sys
import tempfile
import tomllib

from .credentials import CredentialError, private_directory, read_private_json, write_private_json

PROFILE_NAME = '.aot-profile.json'
SHORT_COMMANDS = {'status', 'history', 'pause', 'resume'}
CONNECTED_NEXT_STEP = (
    'Open this exact project folder in Codex and trust it if prompted. Reopen its chat, then ask '
    '"Telegram status" to verify that the native Telegram tools are loaded and connected. '
    'To enable automatic wakeups, ask "Listen for Telegram in this chat for one hour and reply to my messages." '
    'Setup does not start a listener.'
)


def default_state_dir():
    return Path(os.environ.get('XDG_DATA_HOME', str(Path.home() / '.local/share'))) / 'agents-over-telegram'


def _read_local(path):
    """Read bounded owner-controlled configuration, never follow file symlinks."""
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC)
        with os.fdopen(fd, 'r', encoding='utf-8') as stream:
            info = os.fstat(stream.fileno())
            if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_nlink != 1 or info.st_mode & (0o022 if Path(path).name == PROFILE_NAME else 0o002):
                raise CredentialError('unsafe_project_config')
            raw = stream.read(262145)
            if len(raw) > 262144:
                raise CredentialError('invalid_project_config')
            return raw
    except (OSError, UnicodeError):
        raise CredentialError('project_config_unavailable') from None


def _check_directory(path):
    if path.exists() or path.is_symlink():
        info = path.lstat()
        if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o002:
            raise CredentialError('unsafe_project_directory')


def read_profile(project, *, required=False):
    project = Path(project).resolve()
    path = project / PROFILE_NAME
    if not path.exists() and not path.is_symlink():
        if required:
            raise CredentialError('project_profile_missing_run_setup')
        return None
    try:
        value = json.loads(_read_local(path))
        keys = {'version', 'project_dir', 'state_dir', 'client_file', 'client_id', 'label', 'mode'}
        if not isinstance(value, dict) or set(value) != keys or type(value['version']) is not int or value['version'] != 1:
            raise ValueError()
        if value['mode'] not in ('live', 'fake') or not all(isinstance(value[k], str) and value[k] for k in keys - {'version'}):
            raise ValueError()
        for key in ('project_dir', 'state_dir', 'client_file'):
            if not Path(value[key]).is_absolute() or '\x00' in value[key] or str(Path(value[key]).resolve()) != value[key]:
                raise ValueError()
        if value['project_dir'] != str(project):
            raise CredentialError('project_profile_scope_mismatch')
        return value
    except (ValueError, TypeError, KeyError):
        raise CredentialError('invalid_project_profile') from None


def resolve_args(args):
    """Use only the profile in cwd, never a parent or a global identity default."""
    profile = read_profile(Path.cwd())
    client_file = getattr(args, 'client_file', None)
    if profile:
        if args.state_dir is not None and Path(args.state_dir).resolve() != Path(profile['state_dir']):
            raise CredentialError('project_profile_state_mismatch')
        if client_file is not None and Path(client_file).resolve() != Path(profile['client_file']):
            raise CredentialError('project_profile_client_mismatch')
        if args.fake is not None and bool(args.fake) != (profile['mode'] == 'fake'):
            raise CredentialError('project_profile_mode_mismatch')
        args.state_dir = Path(profile['state_dir'])
        args.fake = profile['mode'] == 'fake'
        if hasattr(args, 'client_file') and client_file is None and args.command in SHORT_COMMANDS | {'flush'}:
            args.client_file = Path(profile['client_file'])
        args.expected_client_id = profile['client_id'] if getattr(args, 'client_file', None) is not None else None
    else:
        if args.command in SHORT_COMMANDS and client_file is None:
            raise CredentialError('project_profile_missing_run_setup')
        args.state_dir = args.state_dir or default_state_dir()
        args.fake = bool(args.fake)
        args.expected_client_id = None
    return args


def _entry(state, client, fake):
    args = ['--state-dir', str(state)]
    if fake:
        args.append('--fake')
    args += ['mcp', '--client-file', str(client)]
    return {'command': str(Path(sys.executable).absolute().parent / 'aot'), 'args': args}


def _config_plan(project, expected):
    _check_directory(project)
    folder = project / '.codex'
    _check_directory(folder)
    path = folder / 'config.toml'
    raw = _read_local(path) if path.exists() or path.is_symlink() else None
    try:
        parsed = tomllib.loads(raw or '')
        servers = parsed.get('mcp_servers', {})
        if not isinstance(servers, dict):
            raise ValueError()
        if 'telegram' in servers:
            selected = servers['telegram']
            if not isinstance(selected, dict) or any(selected.get(key) != value for key, value in expected.items()):
                raise CredentialError('project_mcp_config_conflict')
            extras = set(selected) - set(expected)
            if extras - {'startup_timeout_sec', 'tool_timeout_sec'} or any(type(selected[key]) not in (int, float) or not 0 < selected[key] <= 3600 for key in extras):
                raise CredentialError('project_mcp_config_conflict')
            return path, raw, raw
        suffix = '\n[mcp_servers.telegram]\ncommand = ' + json.dumps(expected['command']) + '\nargs = ' + json.dumps(expected['args']) + '\n'
        proposed = (raw or '') + suffix
        if tomllib.loads(proposed)['mcp_servers']['telegram'] != expected:
            raise ValueError()
        return path, raw, proposed
    except (ValueError, TypeError):
        raise CredentialError('invalid_project_config') from None


def _write_local(path, text, previous):
    """Publish a profile/config atomically, preserving existing unrelated contents."""
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    _check_directory(path.parent)
    current = _read_local(path) if path.exists() or path.is_symlink() else None
    if current != previous:
        raise CredentialError('project_config_changed_retry')
    if current == text:
        return
    temp = None
    try:
        fd, temp = tempfile.mkstemp(prefix='.aot-', dir=path.parent)
        with os.fdopen(fd, 'w', encoding='utf-8') as stream:
            stream.write(text)
            stream.flush()
            os.fsync(stream.fileno())
        # Recheck after staging, so a changed file is not silently overwritten.
        actual = _read_local(path) if path.exists() or path.is_symlink() else None
        if actual != previous:
            raise CredentialError('project_config_changed_retry')
        if previous is None:
            os.link(temp, path)
            os.unlink(temp)
        else:
            os.replace(temp, path)
        temp = None
    except OSError:
        raise CredentialError('project_config_write_failed') from None
    finally:
        if temp is not None:
            os.unlink(temp)


def _safe_label(label):
    if not isinstance(label, str) or not 1 <= len(label.strip()) <= 64 or any(not c.isprintable() or c in '[]' for c in label):
        raise CredentialError('invalid_label')
    return label.strip()


def _next_setup(args, state, fake, project, client):
    argv = [_entry(state, client, fake)['command'], '--state-dir', str(state)]
    if fake:
        argv.append('--fake')
    argv += ['setup', '--label', args.label, '--project-dir', str(project)]
    return shlex.join(argv)


def _say(text):
    print(text, file=sys.stderr, flush=True)


def _answer(prompt):
    _say(prompt)
    return input().strip()


def setup(args):
    from .cli import LazyTransport, _client, _mode, register_account
    from .core import Core, CoreError

    args.label = _safe_label(args.label)
    project = Path(args.project_dir).resolve()
    profile = read_profile(project)
    if profile:
        if profile['label'] != args.label:
            raise CredentialError('project_profile_label_mismatch')
        if args.state_dir is not None and Path(args.state_dir).resolve() != Path(profile['state_dir']):
            raise CredentialError('project_profile_state_mismatch')
        if args.client_file is not None and Path(args.client_file).resolve() != Path(profile['client_file']):
            raise CredentialError('project_profile_client_mismatch')
        if args.fake is not None and bool(args.fake) != (profile['mode'] == 'fake'):
            raise CredentialError('project_profile_mode_mismatch')
        state, client, fake = Path(profile['state_dir']), Path(profile['client_file']), profile['mode'] == 'fake'
    else:
        state = Path(args.state_dir or default_state_dir()).resolve()
        fake = bool(args.fake)
        slug = re.sub('[^a-z0-9]+', '-', args.label.lower()).strip('-') or 'agent'
        selected = state / 'clients' / (slug + '-' + hashlib.sha256(str(project).encode()).hexdigest()[:16] + '.json')
        client = Path(args.client_file).resolve() if args.client_file is not None else selected
        if args.client_file is not None and not client.is_file():
            raise CredentialError('existing_client_file_required')
    if state == project or project in state.parents or client == project or project in client.parents:
        raise CredentialError('private_state_must_be_outside_project')
    expected = _entry(state, client, fake)
    config_path, old_config, new_config = _config_plan(project, expected)
    if not args.prepare_only and not sys.stdin.isatty():
        raise CredentialError('private_terminal_required')
    # A mode mismatch is checked before any transport can load credentials.
    state = private_directory(state)
    _mode(state, fake)
    core = Core(state, LazyTransport(state, fake), recover_sends=False)
    try:
        core.accounts_status()
    except CoreError as error:
        if error.code != 'account_not_registered':
            raise
        confirmed = args.receiver_handoff_confirmed
        if not fake and not confirmed and not args.prepare_only:
            _say('This bridge must be the only receiver for the selected bot. Use an unused bot, or stop its existing receiver before continuing. Setup will not stop another receiver or remove a webhook.')
            confirmed = _answer('Confirm that the selected bot has no competing receiver. Type yes to continue:').lower() == 'yes'
        register_account(core, state, fake, confirmed)
    else:
        if not (state / 'mode.json').exists():
            raise CredentialError('state_mode_missing_use_account_register')
        if not fake and not (state / 'credentials.json').is_file():
            raise CredentialError('account_credentials_missing')
    if client.exists() or client.is_symlink():
        client_id = _client(core, client)
        info = core.connection_status(client_id)
        if info['label'] != args.label:
            raise CredentialError('existing_client_label_mismatch')
        if profile and profile['client_id'] != client_id:
            raise CredentialError('project_profile_client_mismatch')
    else:
        if profile:
            raise CredentialError('project_profile_client_missing')
        private_directory(client.parent)
        created = core.create_client(args.label)
        write_private_json(client, created)
        client_id = created['client_id']
    new_profile = {'version': 1, 'project_dir': str(project), 'state_dir': str(state),
                   'client_file': str(client), 'client_id': client_id, 'label': args.label,
                   'mode': 'fake' if fake else 'live'}
    old_profile = _read_local(project / PROFILE_NAME) if profile else None
    # Persist the selected identity before pairing; reruns never mint a replacement.
    _write_local(project / PROFILE_NAME, json.dumps(new_profile, indent=2) + '\n', old_profile)
    _write_local(config_path, new_config, old_config)
    status = core.connection_status(client_id)
    next_setup = _next_setup(args, state, fake, project, client)
    result = {'project_dir': str(project), 'profile_file': str(project / PROFILE_NAME),
              'config_file': str(config_path), 'client_id': client_id, 'label': args.label,
              'mode': new_profile['mode'], 'state_dir': str(state), 'client_file': str(client), 'connected': status['connected'],
              'host_trust_changed': False, 'automatic_delivery': False}
    if status['connected']:
        result['connection'] = status
        result['next_step'] = CONNECTED_NEXT_STEP
        return result
    cache_path = state / 'setup' / (client_id + '.json')
    pairing = status.get('pairing')
    cached = read_private_json(cache_path) if cache_path.exists() else None
    if not pairing:
        cached = core.connection_begin(client_id)
        write_private_json(cache_path, cached, overwrite=cache_path.exists())
        pairing = core.connection_status(client_id)['pairing']
    result['connection'] = core.connection_status(client_id)
    matching = cached is not None and cached.get('pairing_id') == pairing['id']
    command = cached.get('command') if matching else None
    result['pairing_id'] = pairing['id']
    result['pairing'] = {'pairing_id': pairing['id'], 'expires_at': pairing['expires']}
    if command:
        result['pairing_command'] = command
        result['pairing']['command'] = command
    bot_username = (status.get('bot') or {}).get('username')
    bot_name = '@' + bot_username if bot_username else 'the bot'
    destination_step = f'Create or choose this agent\'s dedicated Telegram group or topic and add {bot_name} to its group if it is not already there. '
    if args.prepare_only:
        result['next_step'] = (destination_step + 'Send the pairing_command there, then run: ' + next_setup
                               if command else 'Complete the existing pairing locally, then run: ' + next_setup)
        return result
    _say(f'Setting up {args.label}. The bot registration is shared; this project selects one agent.')
    if pairing.get('sender_id') is None:
        if not command:
            result['next_step'] = 'Complete the existing pairing locally, then run: ' + next_setup
            return result
        _say(destination_step + 'Send this exact command in that group or topic:\n' + command)
        if _answer('Press Enter after sending it, or type cancel to stop:').lower() in {'cancel', 'q', 'quit'}:
            raise KeyboardInterrupt()
        intake = core.collect()
        pairing = core.pairing_status(pairing['id'])
        if pairing.get('sender_id') is None:
            result['intake'] = intake
            result['next_step'] = 'Pairing has not arrived yet. Check the Telegram destination, then run: ' + next_setup
            return result
    _say('Observed pairing: ' + json.dumps({key: pairing.get(key) for key in ('chat_id', 'thread_id', 'sender_id', 'title')}, ensure_ascii=True))
    if _answer('Connect this exact destination and allow this sender? Type yes to confirm:').lower() != 'yes':
        result['next_step'] = 'No destination was confirmed. To review it again, run: ' + next_setup
        return result
    status = core.confirm_pairing(pairing['id'])
    result.update(connected=True, connection=status, next_step=CONNECTED_NEXT_STEP)
    return result


def human_status(value):
    """Compact display derived solely from sanitized public status fields."""
    bot = value.get('bot') or {}
    destination = value.get('destination') or {}
    intake = value.get('intake') or {}
    state = value.get('state') or ('paused' if value.get('paused') else 'connected' if value.get('connected') else 'unpaired')
    lines = [f"{value['label']}: {state}"]
    if bot.get('username'):
        lines.append('Bot: @' + str(bot['username']))
    if destination:
        title = json.dumps(destination.get('title', ''), ensure_ascii=True)
        lines.append(f"Destination: {title} | chat {destination.get('chat_id')} | topic {destination.get('thread_id') or 'none'}")
    senders = value.get('allowed_senders', [])
    lines.append('Allowed senders: ' + (', '.join(str(row['sender_id']) + (' (bot)' if row.get('is_bot') else '') for row in senders) or 'none'))
    lines.append(f"Pending: {value.get('pending_messages', 0)} | outgoing: " + json.dumps(value.get('outbox', {}), sort_keys=True))
    lines.append('Delivery: on demand, or automatically through a running listener.')
    lines.append('Listener: not checked by connection status.')
    lines.append('History: captured context from the last 24 hours.')
    if intake.get('error'):
        lines.append('Intake: ' + str(intake['error']))
    if intake.get('possible_offline_gap'):
        lines.append('History coverage: a collection gap is possible.')
    action = value.get('next_action') or {}
    if action.get('message'):
        lines.append('Next: ' + action['message'])
    return '\n'.join(lines)
