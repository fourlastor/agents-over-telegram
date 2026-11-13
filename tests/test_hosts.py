"""Host integration: setup --host, the runtime pointer, `aot hook`, `aot watch` and the plugin launcher.

Everything is offline: fake transport state in tmp_path, no agent host CLI.
"""
import json
import os
from pathlib import Path
import shutil
import signal
import subprocess
import sys
import time

import pytest

from agents_over_telegram import compat
from agents_over_telegram import cli
from agents_over_telegram.credentials import read_private_json, write_private_json
from agents_over_telegram.hosts import RUNTIME_POINTER, read_runtime_pointer, runtime_pointer
from conftest import posix_only

ROOT = Path(__file__).resolve().parents[1]
LAUNCHER = ROOT / 'scripts' / 'aot_host_launch.py'
SYSTEM_PYTHON = '/usr/bin/python3'
RUNTIME = compat.own_aot()
HOSTS = ['codex', 'claude', 'pi', 'amp']


def invoke(*argv):
    return cli.run(cli.parser().parse_args([str(x) for x in argv]))


def prepare(tmp_path, host, label='Alpha'):
    project = tmp_path / ('project-' + host)
    project.mkdir(mode=0o700, exist_ok=True)
    return invoke('--state-dir', tmp_path / 'state', '--fake', 'setup', '--label', label,
                  '--project-dir', project, '--prepare-only', '--host', host)


def fake_message(state, text, *, sender=123, chat=-100, username='owner_name', edit=None):
    path = Path(state) / 'fake-transport.json'
    fake = read_private_json(path)
    index = max([u['update_id'] for u in fake['updates']] + [0]) + 1
    message = {'message_id': edit or index, 'chat': {'id': chat, 'type': 'supergroup', 'title': 'Test group'},
               'from': {'id': sender, 'is_bot': False, 'username': username}, 'text': text}
    fake['updates'].append({'update_id': index, 'edited_message' if edit else 'message': message})
    write_private_json(path, fake, overwrite=True)


def connected(tmp_path, host='claude'):
    result = prepare(tmp_path, host)
    fake_message(result['state_dir'], result['pairing_command'])
    invoke('--state-dir', result['state_dir'], '--fake', 'collect')
    invoke('--state-dir', result['state_dir'], '--fake', 'pair-confirm', '--pairing-id', result['pairing_id'], '--confirm')
    return result


def receive(result, text='secret message body', **kwargs):
    fake_message(result['state_dir'], text, **kwargs)
    invoke('--state-dir', result['state_dir'], '--fake', 'collect')


def clean_env(**extra):
    env = {key: value for key, value in os.environ.items()
           if key not in {'CLAUDE_PROJECT_DIR', 'AOT_PROJECT_DIR'}}
    env.update(extra)
    return env


@pytest.fixture(autouse=True)
def isolated_cwd(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv('CLAUDE_PROJECT_DIR', raising=False)
    monkeypatch.delenv('AOT_PROJECT_DIR', raising=False)


# ------------------------------------------------------------ setup --host

def secret_free_json_has_telegram(path, host):
    value = json.loads(path.read_text(encoding='utf-8'))
    servers = value['mcpServers' if host == 'pi' else 'amp.mcpServers']
    return servers['telegram']['args'][-2] == '--client-file' and path.stat().st_mode & 0o077 == 0


@posix_only
@pytest.mark.parametrize('host', HOSTS)
def test_setup_host_writes_private_runtime_pointer(tmp_path, host):
    result = prepare(tmp_path, host)
    project = Path(result['project_dir'])
    pointer = project / RUNTIME_POINTER
    assert result['host'] == host
    assert result['runtime_pointer'] == str(pointer)
    assert json.loads(pointer.read_text(encoding='utf-8')) == {'version': 1, 'executable': str(RUNTIME)}
    assert pointer.read_text(encoding='utf-8') == runtime_pointer(RUNTIME)
    assert pointer.stat().st_mode & 0o077 == 0
    assert read_runtime_pointer(project) == RUNTIME
    codex_config = project / '.codex' / 'config.toml'
    if host == 'codex':
        assert result['config_file'] == str(codex_config)
        assert '[mcp_servers.telegram]' in codex_config.read_text(encoding='utf-8')
    else:
        assert not (project / '.codex').exists()
        expected = {'claude': None, 'pi': project / '.pi' / 'mcp.json',
                    'amp': project / '.amp' / 'settings.json'}[host]
        assert result['config_file'] == (str(expected) if expected else None)
        if expected is not None:
            assert secret_free_json_has_telegram(expected, host)
    secret = read_private_json(Path(result['client_file']))['client_secret']
    assert secret not in pointer.read_text(encoding='utf-8')
    # Rerunning setup is idempotent and keeps the same identity and files.
    before = {p: p.read_bytes() for p in project.rglob('*') if p.is_file()}
    again = prepare(tmp_path, host)
    assert again['client_id'] == result['client_id']
    assert before == {p: p.read_bytes() for p in project.rglob('*') if p.is_file()}


@pytest.mark.parametrize('host,marker', [('codex', 'in Codex'), ('claude', 'Claude Code plugin'),
                                         ('pi', 'Pi extension'), ('amp', 'Amp integration')])
def test_setup_host_next_step_matches_host(tmp_path, host, marker):
    connected(tmp_path, host)
    status = prepare(tmp_path, host)
    assert status['connected'] is True
    assert marker in status['next_step']


@posix_only
def test_setup_host_replaces_group_writable_pointer_and_refuses_world_writable(tmp_path):
    result = prepare(tmp_path, 'claude')
    pointer = Path(result['project_dir']) / RUNTIME_POINTER
    pointer.write_text(json.dumps({'version': 1, 'executable': '/elsewhere/aot'}), encoding='utf-8', newline='\n')
    os.chmod(pointer, 0o660)
    assert read_runtime_pointer(Path(result['project_dir'])) is None
    prepare(tmp_path, 'claude')
    assert read_runtime_pointer(Path(result['project_dir'])) == RUNTIME
    assert pointer.stat().st_mode & 0o077 == 0
    os.chmod(pointer, 0o666)
    with pytest.raises(Exception) as error:
        prepare(tmp_path, 'claude')
    assert getattr(error.value, 'code', '') == 'unsafe_project_config'


@pytest.mark.parametrize('content', [
    {'version': True, 'executable': str(RUNTIME)},
    {'version': 2, 'executable': str(RUNTIME)},
    {'version': 1, 'executable': 'relative/aot'},
    {'version': 1, 'executable': 7},
    {'version': 1, 'executable': str(RUNTIME), 'extra': 1},
    [1, 2],
])
def test_read_runtime_pointer_rejects_malformed_content(tmp_path, content):
    (tmp_path / RUNTIME_POINTER).write_text(json.dumps(content), encoding='utf-8', newline='\n')
    os.chmod(tmp_path / RUNTIME_POINTER, 0o600)
    assert read_runtime_pointer(tmp_path) is None


def test_read_runtime_pointer_rejects_symlinks_and_missing(tmp_path):
    assert read_runtime_pointer(tmp_path) is None
    target = tmp_path / 'target.json'
    target.write_text(runtime_pointer(RUNTIME), encoding='utf-8', newline='\n')
    os.chmod(target, 0o600)
    (tmp_path / RUNTIME_POINTER).symlink_to(target)
    assert read_runtime_pointer(tmp_path) is None


# ---------------------------------------------------------------- aot hook

def run_hook(stdin, *args, cwd=None, env=None, host='claude'):
    return subprocess.run([sys.executable, '-m', 'agents_over_telegram', 'hook', '--host', host, *map(str, args)],
                          input=stdin, capture_output=True, text=True, cwd=cwd, env=env or clean_env(), timeout=60)


def test_hook_cli_answers_each_event_without_message_text(tmp_path):
    result = connected(tmp_path)
    project = result['project_dir']
    empty = run_hook(json.dumps({'hook_event_name': 'SessionStart'}), '--project-dir', project)
    assert (empty.returncode, empty.stdout, empty.stderr) == (0, '', '')
    receive(result)
    start = run_hook(json.dumps({'hook_event_name': 'SessionStart', 'source': 'startup'}), '--project-dir', project)
    assert start.returncode == 0
    answer = json.loads(start.stdout)
    text = answer['hookSpecificOutput']['additionalContext']
    assert answer['hookSpecificOutput']['hookEventName'] == 'SessionStart'
    assert '1 new Telegram message' in text and '@owner_name (human)' in text
    assert 'secret message body' not in start.stdout
    prompt = json.loads(run_hook(json.dumps({'hook_event_name': 'UserPromptSubmit', 'prompt': 'x'}),
                                 '--project-dir', project).stdout)
    assert 'still pending' in prompt['hookSpecificOutput']['additionalContext']
    quiet = run_hook(json.dumps({'hook_event_name': 'PostToolUse', 'tool_name': 'Bash'}), '--project-dir', project)
    assert quiet.stdout == ''
    receive(result, 'second body')
    telegram_tool = run_hook(json.dumps({'hook_event_name': 'PostToolUse',
                                         'tool_name': 'mcp__telegram__messages_poll'}), '--project-dir', project)
    assert telegram_tool.stdout == ''
    guarded = run_hook(json.dumps({'hook_event_name': 'Stop', 'stop_hook_active': True}), '--project-dir', project)
    assert guarded.stdout == ''
    stop = run_hook(json.dumps({'hook_event_name': 'Stop', 'stop_hook_active': False}), '--project-dir', project)
    blocked = json.loads(stop.stdout)
    assert blocked['decision'] == 'block' and '(2 pending in total)' in blocked['reason']
    assert run_hook(json.dumps({'hook_event_name': 'Stop', 'stop_hook_active': False}),
                    '--project-dir', project).stdout == ''


def test_hook_cli_finds_project_from_claude_env_and_codex_cwd(tmp_path):
    result = connected(tmp_path)
    receive(result)
    from_env = run_hook(json.dumps({'hook_event_name': 'PostToolUse', 'tool_name': 'Read'}),
                        env=clean_env(CLAUDE_PROJECT_DIR=result['project_dir']), cwd=tmp_path)
    assert 'new Telegram message' in from_env.stdout
    receive(result, 'another')
    from_cwd = run_hook(json.dumps({'hook_event_name': 'PostToolUse', 'tool_name': 'shell',
                                    'cwd': result['project_dir']}), host='codex', cwd=tmp_path)
    assert 'new Telegram message' in from_cwd.stdout


@pytest.mark.parametrize('stdin', ['', 'not json', '[1,2]', '{"hook_event_name": 7}', '{"x": 1}',
                                   '{"hook_event_name": "Stop"', '\x00\xff' * 10])
def test_hook_cli_is_silent_on_garbage(tmp_path, stdin):
    result = connected(tmp_path)
    receive(result)
    process = run_hook(stdin, '--project-dir', result['project_dir'])
    assert (process.returncode, process.stdout, process.stderr) == (0, '', '')


def test_hook_cli_is_silent_on_oversized_input(tmp_path):
    result = connected(tmp_path)
    receive(result)
    big = json.dumps({'hook_event_name': 'SessionStart', 'pad': 'x' * (1024 * 1024 + 10)})
    process = run_hook(big, '--project-dir', result['project_dir'])
    assert (process.returncode, process.stdout) == (0, '')


@posix_only
def test_hook_cli_is_silent_in_unconfigured_or_broken_folders(tmp_path):
    plain = tmp_path / 'plain'
    plain.mkdir()
    event = json.dumps({'hook_event_name': 'SessionStart'})
    for args in (('--project-dir', plain), ()):
        process = run_hook(event, *args, cwd=plain)
        assert (process.returncode, process.stdout, process.stderr) == (0, '', '')
    result = connected(tmp_path)
    receive(result)
    profile = Path(result['project_dir']) / '.aot-profile.json'
    os.chmod(profile, 0o664)
    process = run_hook(event, '--project-dir', result['project_dir'])
    assert (process.returncode, process.stdout) == (0, '')
    os.chmod(profile, 0o600)
    invoke('--state-dir', result['state_dir'], '--fake', 'disconnect', '--client-file', result['client_file'])
    process = run_hook(event, '--project-dir', result['project_dir'])
    assert (process.returncode, process.stdout) == (0, '')


def test_hook_cli_never_fails_on_bad_arguments(tmp_path):
    for argv in (['hook'], ['hook', '--host', 'other'], ['hook', '--host', 'claude', '--unknown-flag'],
                 ['--fake', '--state-dir', str(tmp_path), 'hook', '--bogus']):
        process = subprocess.run([sys.executable, '-m', 'agents_over_telegram', *argv], input='{}',
                                 capture_output=True, text=True, env=clean_env(), timeout=60)
        assert process.returncode == 0 and process.stdout == ''
    # Other commands keep normal argument errors.
    other = subprocess.run([sys.executable, '-m', 'agents_over_telegram', 'status', '--bogus'],
                           capture_output=True, text=True, env=clean_env(), timeout=60)
    assert other.returncode == 2


def test_hook_invocation_detection():
    assert cli._hook_invocation(['hook', '--x'])
    assert cli._hook_invocation(['--state-dir', 'hook', 'hook'])
    assert cli._hook_invocation(['--fake', '--state-dir=/x', 'hook'])
    assert not cli._hook_invocation(['setup', '--label', 'hook'])
    assert not cli._hook_invocation(['--state-dir', 'hook'])
    assert not cli._hook_invocation([])


# --------------------------------------------------------------- aot watch

def run_watch(project, *args, timeout=60):
    return subprocess.run([str(RUNTIME), 'watch', *map(str, args)], capture_output=True, text=True,
                          cwd=project, env=clean_env(), timeout=timeout, stdin=subprocess.DEVNULL)


def test_watch_cli_text_and_jsonl_output(tmp_path):
    result = connected(tmp_path)
    project = result['project_dir']
    receive(result)
    text = run_watch(project, '--duration', '0.5', '--interval', '0.1')
    assert text.returncode == 0, text.stderr
    lines = text.stdout.splitlines()
    assert len(lines) == 1
    assert lines[0].startswith('[Agents over Telegram] 1 new Telegram message for this agent (1 pending in total).')
    assert 'secret message body' not in text.stdout
    receive(result, 'next')
    jsonl = run_watch(project, '--duration', '0.5', '--interval', '0.1', '--format', 'jsonl')
    events = [json.loads(line) for line in jsonl.stdout.splitlines()]
    assert len(events) == 1
    assert events[0]['type'] == 'telegram_messages' and events[0]['new'] == 1 and events[0]['pending'] == 2
    assert 'next' not in json.dumps(events[0]['senders'])
    nothing = run_watch(project, '--duration', '0.3', '--interval', '0.1')
    assert (nothing.returncode, nothing.stdout) == (0, '')


def test_watch_cli_no_backlog_and_collect_in_fake_mode(tmp_path):
    result = connected(tmp_path)
    project = result['project_dir']
    receive(result, 'old')
    quiet = run_watch(project, '--duration', '0.3', '--interval', '0.1', '--no-backlog')
    assert (quiet.returncode, quiet.stdout) == (0, '')
    # Not yet collected: the watcher's own collector must pick it up.
    fake_message(result['state_dir'], 'arrives through the collector')
    started = time.monotonic()
    collected = run_watch(project, '--collect', '--duration', '1', '--interval', '0.2',
                          '--poll-timeout', '5', '--format', 'jsonl')
    assert collected.returncode == 0, collected.stderr
    assert time.monotonic() - started < 15
    events = [json.loads(line) for line in collected.stdout.splitlines()]
    assert [event['new'] for event in events] == [1]
    # The fake transport answers immediately; the watcher paces itself instead of spinning.
    offsets = read_private_json(Path(result['state_dir']) / 'fake-transport.json')['poll_offsets']
    assert len(offsets) < 20


def test_watch_cli_stops_on_revocation_and_needs_a_client(tmp_path):
    result = connected(tmp_path)
    invoke('--state-dir', result['state_dir'], '--fake', 'disconnect', '--client-file', result['client_file'])
    revoked = run_watch(result['project_dir'], '--duration', '0.3')
    assert revoked.returncode == 1
    assert json.loads(revoked.stderr)['error'] == 'authentication_failed'
    plain = tmp_path / 'plain'
    plain.mkdir()
    missing = subprocess.run([str(RUNTIME), '--state-dir', str(tmp_path / 'other'), '--fake', 'watch', '--duration', '0.1'],
                             capture_output=True, text=True, cwd=plain, env=clean_env(), timeout=60)
    assert missing.returncode == 1 and json.loads(missing.stderr)['error'] == 'client_file_required'


def test_watch_cli_reports_revocation_while_running(tmp_path):
    result = connected(tmp_path)
    process = subprocess.Popen([str(RUNTIME), 'watch', '--interval', '0.1'], cwd=result['project_dir'],
                               stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, env=clean_env(),
                               stdin=subprocess.DEVNULL)
    try:
        time.sleep(0.8)
        invoke('--state-dir', result['state_dir'], '--fake', 'disconnect', '--client-file', result['client_file'])
        out, err = process.communicate(timeout=30)
    finally:
        if process.poll() is None:
            process.kill()
    assert process.returncode == 0, err
    assert out.strip() == '[Agents over Telegram] Watching stopped: client_revoked.'


# ------------------------------------------------------------ the launcher

needs_system_python = pytest.mark.skipif(not os.access(SYSTEM_PYTHON, os.X_OK), reason='needs /usr/bin/python3')


def launch(project, *args, stdin='', cwd=None, env_project=True, timeout=60):
    env = clean_env(AOT_PROJECT_DIR=str(project)) if env_project else clean_env()
    return subprocess.run([SYSTEM_PYTHON, str(LAUNCHER), *map(str, args)], input=stdin, capture_output=True,
                          text=True, cwd=cwd or project, env=env, timeout=timeout)


def write_pointer(project, content, mode=0o600):
    path = Path(project) / RUNTIME_POINTER
    if path.exists() or path.is_symlink():
        path.unlink()
    path.write_text(content if isinstance(content, str) else json.dumps(content), encoding='utf-8', newline='\n')
    os.chmod(path, mode)
    return path


def stub_runtime(tmp_path, mode=0o700):
    """An executable that records its arguments and working folder."""
    path = tmp_path / 'bin' / 'aot-stub'
    path.parent.mkdir(exist_ok=True)
    path.write_text('#!/bin/sh\nprintf "%s\\n" "$PWD" "$@"\n', encoding='utf-8', newline='\n')
    os.chmod(path, mode)
    return path


@needs_system_python
def test_launcher_without_pointer(tmp_path):
    plain = tmp_path / 'plain'
    plain.mkdir()
    # The MCP relay stays up for a later setup; with no host input it simply ends.
    mcp = launch(plain, 'mcp')
    assert (mcp.returncode, mcp.stdout) == (0, '') and 'not set up for this project folder' in mcp.stderr
    watch = launch(plain, 'watch')
    assert (watch.returncode, watch.stdout, watch.stderr) == (0, '', '')
    hook = launch(plain, 'hook', '--host', 'claude', stdin='{"hook_event_name":"Stop"}' * 10000)
    assert (hook.returncode, hook.stdout, hook.stderr) == (0, '', '')
    usage = launch(plain, 'status')
    assert usage.returncode == 2 and 'Usage' in usage.stderr
    assert launch(plain).returncode == 2


@needs_system_python
def test_launcher_idles_in_unconfigured_folder_when_asked(tmp_path):
    plain = tmp_path / 'plain'
    plain.mkdir()
    process = subprocess.Popen([SYSTEM_PYTHON, str(LAUNCHER), 'watch', '--idle-if-unconfigured'], cwd=plain,
                               env=clean_env(AOT_PROJECT_DIR=str(plain)), stdout=subprocess.PIPE,
                               stderr=subprocess.PIPE, stdin=subprocess.DEVNULL)
    try:
        time.sleep(1.0)
        assert process.poll() is None
        process.send_signal(signal.SIGINT)
        out, err = process.communicate(timeout=10)
        assert (process.returncode, out, err) == (0, b'', b'')
    finally:
        if process.poll() is None:
            process.kill()
    # The flag never makes mcp or hook wait.
    assert launch(plain, 'mcp', '--idle-if-unconfigured').returncode == 0
    assert launch(plain, 'hook', '--idle-if-unconfigured', timeout=10).returncode == 0


@needs_system_python
def test_launcher_idle_flag_is_stripped_and_hook_gets_project(tmp_path):
    result = prepare(tmp_path, 'claude')
    project = Path(result['project_dir'])
    stub = stub_runtime(tmp_path)
    write_pointer(project, {'version': 1, 'executable': str(stub)})
    elsewhere = tmp_path / 'elsewhere'
    elsewhere.mkdir()
    watch = launch(project, 'watch', '--idle-if-unconfigured', '--format', 'jsonl', cwd=elsewhere)
    assert watch.returncode == 0, watch.stderr
    assert watch.stdout.splitlines() == [str(project), 'watch', '--format', 'jsonl']
    hook = launch(project, 'hook', '--host', 'claude', cwd=elsewhere)
    assert hook.stdout.splitlines() == [str(project), 'hook', '--host', 'claude', '--project-dir', str(project)]


@needs_system_python
def test_launcher_uses_claude_project_dir_then_cwd(tmp_path):
    result = prepare(tmp_path, 'claude')
    project = Path(result['project_dir'])
    stub = stub_runtime(tmp_path)
    write_pointer(project, {'version': 1, 'executable': str(stub)})
    via_claude = subprocess.run([SYSTEM_PYTHON, str(LAUNCHER), 'watch'], capture_output=True, text=True,
                                cwd=tmp_path, env=clean_env(CLAUDE_PROJECT_DIR=str(project)), timeout=60)
    assert via_claude.stdout.splitlines()[0] == str(project)
    via_cwd = launch(project, 'watch', env_project=False)
    assert via_cwd.stdout.splitlines()[0] == str(project)
    # A subfolder is not the project: no parent search.
    sub = project / 'src'
    sub.mkdir()
    nested = launch(sub, 'mcp', env_project=False)
    assert nested.returncode == 0 and 'not set up' in nested.stderr


@needs_system_python
@pytest.mark.parametrize('problem', ['group_writable', 'world_writable', 'symlink', 'hardlink', 'relative',
                                     'version_true', 'extra_key', 'not_json', 'missing_runtime',
                                     'world_writable_runtime', 'no_profile', 'unsafe_profile'])
def test_launcher_refuses_unsafe_pointer_or_runtime(tmp_path, problem):
    result = prepare(tmp_path, 'claude')
    project = Path(result['project_dir'])
    stub = stub_runtime(tmp_path)
    good = {'version': 1, 'executable': str(stub)}
    pointer = write_pointer(project, good)
    if problem == 'group_writable':
        os.chmod(pointer, 0o620)
    elif problem == 'world_writable':
        os.chmod(pointer, 0o602)
    elif problem == 'symlink':
        target = tmp_path / 'target.json'
        shutil.move(pointer, target)
        pointer.symlink_to(target)
    elif problem == 'hardlink':
        os.link(pointer, tmp_path / 'second-link.json')
    elif problem == 'relative':
        write_pointer(project, {'version': 1, 'executable': 'bin/aot-stub'})
    elif problem == 'version_true':
        write_pointer(project, {'version': True, 'executable': str(stub)})
    elif problem == 'extra_key':
        write_pointer(project, dict(good, args=['--evil']))
    elif problem == 'not_json':
        write_pointer(project, '{not json')
    elif problem == 'missing_runtime':
        stub.unlink()
    elif problem == 'world_writable_runtime':
        os.chmod(stub, 0o777)
    elif problem == 'no_profile':
        (project / '.aot-profile.json').unlink()
    elif problem == 'unsafe_profile':
        os.chmod(project / '.aot-profile.json', 0o660)
    mcp = launch(project, 'mcp')
    assert mcp.returncode == 0
    assert 'missing or unsafe' in mcp.stderr and mcp.stdout == ''
    watch = launch(project, 'watch')
    assert (watch.returncode, watch.stdout) == (0, '')
    hook = launch(project, 'hook', '--host', 'claude', stdin='{}')
    assert (hook.returncode, hook.stdout) == (0, '')


@needs_system_python
def test_launcher_exec_failure_messages(tmp_path):
    result = prepare(tmp_path, 'claude')
    project = Path(result['project_dir'])
    broken = tmp_path / 'bin' / 'broken'
    broken.parent.mkdir()
    broken.write_text('#!/nonexistent/interpreter\n', encoding='utf-8', newline='\n')
    os.chmod(broken, 0o700)
    write_pointer(project, {'version': 1, 'executable': str(broken)})
    mcp = launch(project, 'mcp')
    assert mcp.returncode == 0 and 'could not be started' in mcp.stderr
    hook = launch(project, 'hook', '--host', 'claude', stdin='{}')
    assert (hook.returncode, hook.stdout) == (0, '')


@needs_system_python
def test_launcher_runs_real_watch_and_hook(tmp_path):
    result = connected(tmp_path)
    project = Path(result['project_dir'])
    assert read_runtime_pointer(project) == RUNTIME
    receive(result)
    elsewhere = tmp_path / 'elsewhere'
    elsewhere.mkdir()
    watch = launch(project, 'watch', '--idle-if-unconfigured', '--duration', '0.5', '--interval', '0.1',
                   '--format', 'jsonl', cwd=elsewhere)
    assert watch.returncode == 0, watch.stderr
    events = [json.loads(line) for line in watch.stdout.splitlines()]
    assert len(events) == 1 and events[0]['new'] == 1
    receive(result, 'two')
    hook = launch(project, 'hook', '--host', 'claude', cwd=elsewhere,
                  stdin=json.dumps({'hook_event_name': 'Stop', 'stop_hook_active': False}))
    assert hook.returncode == 0, hook.stderr
    assert json.loads(hook.stdout)['decision'] == 'block'


@pytest.mark.parametrize('host', HOSTS)
def test_pairing_follow_up_command_keeps_host(tmp_path, host):
    result = prepare(tmp_path, host)
    assert result['next_step'].endswith('--host ' + host)


def test_rendered_codex_plugin_has_no_dangling_hooks(tmp_path):
    result = connected(tmp_path, 'codex')
    output = tmp_path / 'rendered'
    cli.render_plugin(result['state_dir'], result['client_file'], output, 'aot-rendered')
    manifest = json.loads((output / '.codex-plugin' / 'plugin.json').read_text(encoding='utf-8'))
    assert 'hooks' not in manifest
    assert json.loads((ROOT / '.codex-plugin' / 'plugin.json').read_text(encoding='utf-8'))['hooks'] == './codex/hooks.json'


# ----------------------------------------------------------------- aot wake

def run_wake(*args, cwd=None, timeout=60):
    return subprocess.run([str(RUNTIME), 'wake', '--host', 'claude', *map(str, args)], capture_output=True,
                          text=True, cwd=cwd, env=clean_env(), timeout=timeout, stdin=subprocess.DEVNULL)


def wake_log(result):
    """The waiter's start and exit lines, oldest first."""
    path = Path(result['state_dir']) / 'wake.log'
    if not path.exists():
        return []
    if os.name != 'nt':
        assert path.stat().st_mode & 0o777 == 0o600
    return [json.loads(line) for line in path.read_text(encoding='utf-8').splitlines()]


def exits(result):
    return [line['reason'] for line in wake_log(result) if line['event'] == 'exit']


def test_wake_exits_2_with_a_notice_when_a_message_waits(tmp_path):
    result = connected(tmp_path)
    project = result['project_dir']
    receive(result)
    woken = run_wake('--project-dir', project, '--duration', '5')
    assert woken.returncode == 2 and woken.stdout == ''
    assert woken.stderr.startswith('[Agents over Telegram] 1 new Telegram message for this agent (1 pending in total).')
    assert 'secret message body' not in woken.stderr
    # The delivery is announced once; with nothing new the waiter ends quietly.
    quiet = run_wake('--project-dir', project, '--duration', '0.5')
    assert (quiet.returncode, quiet.stdout, quiet.stderr) == (0, '', '')
    # Each start and exit is logged with its reason, never the message text.
    assert [line['event'] for line in wake_log(result)] == ['start', 'exit', 'start', 'exit']
    assert exits(result) == ['woken', 'watch_ended:duration_elapsed']
    assert 'secret message body' not in (Path(result['state_dir']) / 'wake.log').read_text(encoding='utf-8')


def keep_alive(result, value='on'):
    # Turning it on needs the owner; here the owner confirms at a terminal.
    original = cli._confirm_or_ask_owner
    cli._confirm_or_ask_owner = lambda *args: None
    try:
        invoke('--state-dir', result['state_dir'], '--fake', 'delivery-settings', '--client-file', result['client_file'],
               '--keep-alive', value)
    finally:
        cli._confirm_or_ask_owner = original


def test_keep_alive_wakes_the_session_once_at_the_time_limit(tmp_path):
    result = connected(tmp_path)
    keep_alive(result)
    refresh = run_wake('--project-dir', result['project_dir'], '--duration', '0.5')
    assert (refresh.returncode, refresh.stdout) == (2, '')
    assert refresh.stderr.startswith('[Agents over Telegram] No new Telegram messages.')
    assert 'end this turn now' in refresh.stderr
    keep_alive(result, 'off')
    quiet = run_wake('--project-dir', result['project_dir'], '--duration', '0.5')
    assert (quiet.returncode, quiet.stderr) == (0, '')
    assert exits(result) == ['keep_alive_refresh', 'watch_ended:duration_elapsed']


def test_keep_alive_is_quiet_when_the_host_is_gone_or_a_newer_waiter_runs(tmp_path):
    result = connected(tmp_path)
    keep_alive(result)
    gone = subprocess.Popen([sys.executable, '-c', 'pass'])
    gone.wait()
    process = run_wake('--project-dir', result['project_dir'], '--duration', '0.5', '--host-pid', gone.pid)
    assert (process.returncode, process.stdout, process.stderr) == (0, '', '')
    assert exits(result) == ['host_gone']
    # A waiter that handed over to a newer one never wakes the session.
    first = start_wake(result['project_dir'], 'kept', duration=60)
    second = None
    try:
        wait_for_owner(result, 'kept', first)
        second = start_wake(result['project_dir'], 'kept', duration=60)
        out, err = first.communicate(timeout=30)
        assert (first.returncode, out, err) == (0, '', '')
        assert exits(result)[-1] == 'replaced'
    finally:
        for waiter in (first, second):
            if waiter is not None and waiter.poll() is None:
                waiter.kill()
                waiter.communicate(timeout=10)


def test_wake_collects_from_telegram_while_waiting(tmp_path):
    result = connected(tmp_path)
    project = result['project_dir']
    waiter = subprocess.Popen([str(RUNTIME), 'wake', '--host', 'claude', '--project-dir', project,
                               '--duration', '30'], stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                              stdin=subprocess.DEVNULL, env=clean_env(), text=True)
    try:
        time.sleep(1.0)
        assert waiter.poll() is None
        # Only the fake Telegram has the message; nothing else collects it.
        fake_message(result['state_dir'], 'arrives later')
        out, err = waiter.communicate(timeout=30)
    finally:
        if waiter.poll() is None:
            waiter.kill()
    assert waiter.returncode == 2 and out == ''
    assert '1 new Telegram message' in err and 'arrives later' not in err


def wait_for_owner(result, session, process, previous=None, timeout=30):
    """Wait until a waiter has registered as its session's owner; return its token.

    A waiter writes its owner token as it starts; one started after it takes
    over. Starting the next waiter before that would make the order of the two
    a race on a slow machine. The token is compared with the previous owner's,
    not with the process id: on Windows the venv's aot.exe runs Python as a
    child process, so the waiter's own pid differs from the one started here.
    """
    from agents_over_telegram.hosts import wake_key
    owner = Path(result['state_dir']) / 'wake-locks' / f"{wake_key(result['client_id'], session)}.owner"
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        token = owner.read_text(encoding='utf-8').strip() if owner.exists() else ''
        if token and token != previous:
            return token
        assert process.poll() is None, process.communicate()
        time.sleep(0.05)
    pytest.fail('the waiter never registered as its session owner')

def retire(result, session, process, timeout=60):
    """End a waiter the way a newer turn would: a new owner token, then a clean exit.

    Killing it instead is TerminateProcess on Windows, which can stop it while
    it holds the collector lock. Windows releases a dead process's byte locks
    only when it gets around to it, so the next waiter could find collection
    busy for longer than a test waits.
    """
    from agents_over_telegram.hosts import _write_owner, wake_key
    owner = Path(result['state_dir']) / 'wake-locks' / f"{wake_key(result['client_id'], session)}.owner"
    _write_owner(owner, 'retired-by-test')
    return process.communicate(timeout=timeout)


def wake_log_text(result):
    path = Path(result['state_dir']) / 'wake.log'
    return path.read_text(encoding='utf-8') if path.exists() else '(no wake.log)'


def start_wake(project, session, duration=60):
    return subprocess.Popen([str(RUNTIME), 'wake', '--host', 'claude', '--project-dir', project,
                             '--session', session, '--duration', str(duration)], stdout=subprocess.PIPE,
                            stderr=subprocess.PIPE, stdin=subprocess.DEVNULL, env=clean_env(), text=True)


def test_newest_waiter_takes_over_its_session(tmp_path):
    result = connected(tmp_path)
    project = result['project_dir']
    first = start_wake(project, 'abc')
    other = start_wake(project, 'other')
    second = None
    try:
        first_token = wait_for_owner(result, 'abc', first)
        wait_for_owner(result, 'other', other)
        assert first.poll() is None
        # A turn end starts a new waiter; the running one hands over and exits quietly.
        second = start_wake(project, 'abc')
        out, err = first.communicate(timeout=30)
        assert (first.returncode, out, err) == (0, '', '')
        wait_for_owner(result, 'abc', second, previous=first_token)
        assert second.poll() is None
        # Another session keeps its own waiter.
        assert other.poll() is None
        # Both sessions share a client; only one waiter may see the next message.
        out, err = retire(result, 'other', other)
        assert (other.returncode, out, err) == (0, '', '')
        fake_message(result['state_dir'], 'after handover')
        try:
            out, err = second.communicate(timeout=60)
        except subprocess.TimeoutExpired:
            pytest.fail('the new waiter did not report the message; wake.log:\n' + wake_log_text(result))
        assert second.returncode == 2 and '1 new Telegram message' in err
    finally:
        for process in (first, second, other):
            if process is not None and process.poll() is None:
                process.kill()
                process.communicate(timeout=10)


@pytest.mark.skipif(os.name == 'nt', reason='POSIX signals')
def test_wake_logs_a_signal_and_still_dies_of_it(tmp_path):
    import signal
    result = connected(tmp_path)
    waiter = start_wake(result['project_dir'], 'sig')
    try:
        wait_for_owner(result, 'sig', waiter)
        waiter.send_signal(signal.SIGTERM)
        out, err = waiter.communicate(timeout=30)
    finally:
        if waiter.poll() is None:
            waiter.kill()
            waiter.communicate(timeout=10)
    assert waiter.returncode == -signal.SIGTERM
    assert exits(result) == ['signal:SIGTERM']


def test_wake_is_silent_in_unconfigured_folders_and_on_bad_arguments(tmp_path):
    plain = tmp_path / 'plain'
    plain.mkdir()
    for args in (('--project-dir', plain), ()):
        process = run_wake(*args, '--duration', '0.5', cwd=plain)
        assert (process.returncode, process.stdout, process.stderr) == (0, '', '')
    result = connected(tmp_path)
    receive(result)
    invoke('--state-dir', result['state_dir'], '--fake', 'disconnect', '--client-file', result['client_file'])
    revoked = run_wake('--project-dir', result['project_dir'], '--duration', '0.5')
    assert (revoked.returncode, revoked.stderr) == (0, '')
    for argv in (['wake'], ['wake', '--host', 'codex'], ['wake', '--host', 'claude', '--bogus']):
        process = subprocess.run([sys.executable, '-m', 'agents_over_telegram', *argv], capture_output=True,
                                 text=True, env=clean_env(), timeout=60, stdin=subprocess.DEVNULL)
        assert (process.returncode, process.stdout) == (0, '')
    assert cli._hook_invocation(['--fake', 'wake', '--x'])


def wake_stub(tmp_path, body):
    path = tmp_path / 'bin' / 'aot-wake-stub'
    path.parent.mkdir(exist_ok=True)
    path.write_text('#!/bin/sh\n' + body, encoding='utf-8', newline='\n')
    os.chmod(path, 0o700)
    return path


@needs_system_python
def test_launcher_wake_passes_project_session_and_host(tmp_path):
    result = prepare(tmp_path, 'claude')
    project = Path(result['project_dir'])
    stub = wake_stub(tmp_path, 'printf "[Agents over Telegram] " >&2\nprintf "%s|" "$PWD" "$@" >&2\nexit 2\n')
    write_pointer(project, {'version': 1, 'executable': str(stub)})
    elsewhere = tmp_path / 'elsewhere'
    elsewhere.mkdir()
    wake = launch(project, 'wake', '--host', 'claude', cwd=elsewhere,
                  stdin=json.dumps({'hook_event_name': 'Stop', 'session_id': 'ab-12_c'}))
    assert wake.returncode == 2 and wake.stdout == ''
    parts = wake.stderr.removeprefix('[Agents over Telegram] ').split('|')
    assert parts[:6] == [str(project), 'wake', '--host', 'claude', '--project-dir', str(project)]
    assert parts[6] == '--host-pid' and int(parts[7]) > 1
    assert parts[8:10] == ['--session', 'ab-12_c']
    odd = launch(project, 'wake', '--host', 'claude', cwd=elsewhere, stdin=json.dumps({'session_id': '../x y'}))
    assert '--session' not in odd.stderr
    plain = tmp_path / 'plain'
    plain.mkdir()
    assert launch(plain, 'wake', '--host', 'claude', stdin='{}').returncode == 0
    broken = subprocess.run([SYSTEM_PYTHON, str(LAUNCHER), 'bogus'], capture_output=True, text=True, timeout=10)
    assert broken.returncode == 2


@needs_system_python
@pytest.mark.parametrize('body', [
    # A runtime without `aot wake` fails argument parsing with status 2.
    'echo "usage: aot [-h] ... invalid choice: wake" >&2\nexit 2\n',
    'exit 1\n',
    'echo "[Agents over Telegram] fine" >&2\nexit 0\n',
])
def test_launcher_wake_never_wakes_without_a_notice(tmp_path, body):
    result = prepare(tmp_path, 'claude')
    project = Path(result['project_dir'])
    write_pointer(project, {'version': 1, 'executable': str(wake_stub(tmp_path, body))})
    wake = launch(project, 'wake', '--host', 'claude', stdin='{}')
    assert (wake.returncode, wake.stdout, wake.stderr) == (0, '', '')


@needs_system_python
def test_launcher_runs_real_wake(tmp_path):
    result = connected(tmp_path)
    project = Path(result['project_dir'])
    receive(result)
    wake = launch(project, 'wake', '--host', 'claude', '--duration', '5', cwd=tmp_path,
                  stdin=json.dumps({'hook_event_name': 'SessionStart', 'session_id': 's1'}))
    assert wake.returncode == 2, wake.stderr
    assert '1 new Telegram message' in wake.stderr


def test_wake_stops_when_the_host_is_gone(tmp_path):
    result = connected(tmp_path)
    gone = subprocess.Popen(['true'])
    gone.wait()
    started = time.monotonic()
    process = run_wake('--project-dir', result['project_dir'], '--host-pid', gone.pid, '--duration', '60')
    assert (process.returncode, process.stderr) == (0, '')
    assert time.monotonic() - started < 30


def test_waiting_waiter_keeps_waiting_until_replaced(tmp_path):
    from agents_over_telegram import compat
    import hashlib
    result = connected(tmp_path)
    from agents_over_telegram.hosts import wake_key
    key = wake_key(result['client_id'], 'abc')
    locks = Path(result['state_dir']) / 'wake-locks'
    locks.mkdir(mode=0o700, exist_ok=True)
    # Stand in for an old waiter that is slow to notice the handover.
    with open(locks / f'{key}.lock', 'a+') as held:
        compat.lock(held, blocking=True)
        waiting = start_wake(result['project_dir'], 'abc')
        try:
            # It registers as owner, then waits for the lock held above.
            wait_for_owner(result, 'abc', waiting)
            assert waiting.poll() is None
            # A newer owner replaces the waiting one, which then exits quietly.
            (locks / f'{key}.owner').write_text('newer', encoding='utf-8', newline='\n')
            out, err = waiting.communicate(timeout=10)
            assert (waiting.returncode, out, err) == (0, '', '')
        finally:
            if waiting.poll() is None:
                waiting.kill()
                waiting.communicate(timeout=10)


# ------------------------------------------------------------ the MCP relay

FAKE_MCP = '''#!{python}
import json, os, sys, time
log = open({log!r}, "a")
with open({pids!r}, "a") as pids:
    pids.write(str(os.getpid()) + "\\n")
for line in sys.stdin:
    message = json.loads(line)
    log.write(json.dumps({{"version": {version!r}, "message": message}}) + "\\n")
    log.flush()
    method, ident = message.get("method"), message.get("id")
    if method == "crash":
        sys.exit(3)
    if ident is None:
        continue
    if method == "slow":
        time.sleep(1.5)
    if method == "hang":
        time.sleep(60)
    if method == "initialize" and {version!r} == "mute":
        sys.exit(1)
    if method == "initialize":
        result = {{"protocolVersion": "2025-06-18", "capabilities": {{"tools": {{}}}},
                   "serverInfo": {{"name": "fake", "version": {version!r}}}}}
    else:
        result = {{"version": {version!r}, "method": method}}
    sys.stdout.write(json.dumps({{"jsonrpc": "2.0", "id": ident, "result": result}}) + "\\n")
    sys.stdout.flush()
    if method == "initialize" and {version!r} == "crashy":
        sys.exit(1)
'''


def fake_mcp_runtime(tmp_path, version):
    path = tmp_path / 'bin' / ('aot-' + version)
    path.parent.mkdir(exist_ok=True)
    path.write_text(FAKE_MCP.format(python=sys.executable, log=str(tmp_path / 'mcp.log'),
                                    pids=str(tmp_path / 'pids'), version=version), encoding='utf-8', newline='\n')
    os.chmod(path, 0o700)
    return path


class RelaySession:
    """A host talking newline-delimited JSON-RPC to the launcher's mcp relay."""

    def __init__(self, project):
        import queue
        import threading
        self.process = subprocess.Popen([SYSTEM_PYTHON, str(LAUNCHER), 'mcp'], cwd=project,
                                        env=clean_env(AOT_PROJECT_DIR=str(project)), stdin=subprocess.PIPE,
                                        stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        self.lines = queue.Queue()
        threading.Thread(target=self._read, daemon=True).start()

    def _read(self):
        for line in iter(self.process.stdout.readline, b''):
            self.lines.put(json.loads(line))

    def send(self, ident, method, **params):
        message = {'jsonrpc': '2.0', 'method': method, 'params': params}
        if ident is not None:
            message['id'] = ident
        self.process.stdin.write(json.dumps(message).encode() + b'\n')
        self.process.stdin.flush()

    def receive(self):
        return self.lines.get(timeout=20)

    def call(self, ident, method):
        self.send(ident, method)
        return self.receive()

    def close(self):
        self.process.stdin.close()
        try:
            return self.process.wait(timeout=20)
        finally:
            if self.process.poll() is None:
                self.process.kill()


def relay_log(tmp_path):
    return [json.loads(line) for line in (tmp_path / 'mcp.log').read_text(encoding='utf-8').splitlines()]


def relay_project(tmp_path):
    project = Path(prepare(tmp_path, 'claude')['project_dir'])
    write_pointer(project, {'version': 1, 'executable': str(fake_mcp_runtime(tmp_path, 'v1'))})
    session = RelaySession(project)
    session.send(1, 'initialize', protocolVersion='2025-06-18')
    init = session.receive()
    session.send(None, 'notifications/initialized')
    return project, session, init


@needs_system_python
def test_relay_passes_the_session_through_and_announces_tool_changes(tmp_path):
    project, session, init = relay_project(tmp_path)
    try:
        assert init['id'] == 1 and init['result']['serverInfo']['version'] == 'v1'
        assert init['result']['capabilities']['tools'] == {'listChanged': True}
        assert session.call(2, 'tools/list')['result'] == {'version': 'v1', 'method': 'tools/list'}
    finally:
        assert session.close() == 0
    methods = [entry['message'].get('method') for entry in relay_log(tmp_path)]
    assert methods == ['initialize', 'notifications/initialized', 'tools/list']


@needs_system_python
def test_relay_moves_to_a_new_runtime_without_a_new_session(tmp_path):
    project, session, _ = relay_project(tmp_path)
    try:
        assert session.call(2, 'tools/list')['result']['version'] == 'v1'
        write_pointer(project, {'version': 1, 'executable': str(fake_mcp_runtime(tmp_path, 'v2'))})
        session.send(3, 'tools/list')
        assert session.receive() == {'jsonrpc': '2.0', 'method': 'notifications/tools/list_changed'}
        assert session.receive() == {'jsonrpc': '2.0', 'id': 3, 'result': {'version': 'v2', 'method': 'tools/list'}}
    finally:
        assert session.close() == 0
    v2 = [entry['message'] for entry in relay_log(tmp_path) if entry['version'] == 'v2']
    # The new runtime gets the host's own initialize under a relay id, then initialized.
    assert [m.get('method') for m in v2] == ['initialize', 'notifications/initialized', 'tools/list']
    assert v2[0]['params'] == {'protocolVersion': '2025-06-18'} and v2[0]['id'] != 1


@needs_system_python
def test_relay_waits_for_requests_in_flight_before_swapping(tmp_path):
    project, session, _ = relay_project(tmp_path)
    try:
        session.send(2, 'slow')
        time.sleep(0.3)
        write_pointer(project, {'version': 1, 'executable': str(fake_mcp_runtime(tmp_path, 'v2'))})
        # Still busy: this request goes to the old runtime too.
        session.send(3, 'tools/list')
        answers = {}
        while len(answers) < 2:
            message = session.receive()
            answers[message['id']] = message['result']['version']
        assert answers == {2: 'v1', 3: 'v1'}
        assert session.call(4, 'tools/list')['method'] == 'notifications/tools/list_changed'
        assert session.receive()['result']['version'] == 'v2'
    finally:
        assert session.close() == 0


@needs_system_python
def test_relay_swaps_past_a_call_the_host_cancelled(tmp_path):
    project, session, _ = relay_project(tmp_path)
    try:
        session.send(2, 'hang')
        time.sleep(0.3)
        session.send(None, 'notifications/cancelled', requestId=2, reason='timeout')
        write_pointer(project, {'version': 1, 'executable': str(fake_mcp_runtime(tmp_path, 'v2'))})
        assert session.call(3, 'tools/list')['method'] == 'notifications/tools/list_changed'
        assert session.receive() == {'jsonrpc': '2.0', 'id': 3, 'result': {'version': 'v2', 'method': 'tools/list'}}
    finally:
        assert session.close() == 0


def alive(pid):
    from agents_over_telegram import compat
    if compat.WINDOWS:
        # os.kill(pid, 0) would terminate it there.
        return compat.pid_alive(pid)
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    # A zombie still answers kill(0); it is gone once its parent reaped it.
    try:
        return Path(f'/proc/{pid}/stat').read_text(encoding='utf-8').split()[2] != 'Z'
    except OSError:
        return False


@needs_system_python
@pytest.mark.parametrize('signum', [signal.SIGTERM, getattr(signal, 'SIGHUP', signal.SIGTERM)])
def test_relay_stops_the_runtime_when_the_host_stops_it(tmp_path, signum):
    project, session, _ = relay_project(tmp_path)
    session.send(2, 'hang')
    pids = tmp_path / 'pids'
    deadline = time.monotonic() + 30
    while not (pids.exists() and pids.read_text(encoding='utf-8').strip()) and time.monotonic() < deadline:
        time.sleep(0.05)
    [pid] = [int(line) for line in pids.read_text(encoding='utf-8').split()]
    session.process.send_signal(signum)
    assert session.process.wait(timeout=10) == 128 + signum
    deadline = time.monotonic() + 10
    while alive(pid) and time.monotonic() < deadline:
        time.sleep(0.1)
    assert not alive(pid)


@needs_system_python
def test_relay_answers_lost_calls_and_restarts_a_stopped_runtime(tmp_path):
    project, session, _ = relay_project(tmp_path)
    try:
        session.send(2, 'crash')
        lost = session.receive()
        assert lost['id'] == 2 and lost['error']['code'] == -32603
        assert session.call(3, 'tools/list')['method'] == 'notifications/tools/list_changed'
        assert session.receive() == {'jsonrpc': '2.0', 'id': 3, 'result': {'version': 'v1', 'method': 'tools/list'}}
    finally:
        assert session.close() == 0


@needs_system_python
def test_relay_refuses_calls_while_the_runtime_cannot_start(tmp_path):
    project, session, _ = relay_project(tmp_path)
    try:
        session.send(2, 'crash')
        session.receive()
        broken = tmp_path / 'bin' / 'aot-v1'
        os.chmod(broken, 0o777)  # unsafe now, so the launcher will not start it
        error = session.call(3, 'tools/call')
        assert error['id'] == 3 and error['error']['code'] == -32603
        os.chmod(broken, 0o700)
        assert session.call(4, 'tools/list')['method'] == 'notifications/tools/list_changed'
        assert session.receive()['id'] == 4
    finally:
        assert session.close() == 0


@needs_system_python
def test_relay_serves_the_real_runtime(tmp_path):
    project = Path(connected(tmp_path)['project_dir'])
    session = RelaySession(project)
    try:
        session.send(1, 'initialize', protocolVersion='2025-06-18', capabilities={},
                     clientInfo={'name': 'test-host', 'version': '1'})
        init = session.receive()
        assert init['id'] == 1 and init['result']['capabilities']['tools']['listChanged'] is True
        session.send(None, 'notifications/initialized')
        tools = {tool['name'] for tool in session.call(2, 'tools/list')['result']['tools']}
        assert {'messages_poll', 'message_send', 'messages_ack', 'connection_status'} <= tools
        session.send(3, 'tools/call', name='connection_status', arguments={})
        status = json.loads(session.receive()['result']['content'][0]['text'])
        assert status['ok'] and status['result']['connected'] is True
    finally:
        assert session.close() == 0


def test_setup_from_a_newer_runtime_moves_a_paired_project_to_it(tmp_path, monkeypatch):
    """The update path: the new runtime's setup --prepare-only repoints the project and keeps its pairing."""
    result = connected(tmp_path)
    project = Path(result['project_dir'])
    newer = tmp_path / 'newer' / 'bin'
    newer.mkdir(parents=True)
    aot = newer / compat.executable_name('aot')
    aot.write_text('#!/bin/sh\n', encoding='utf-8', newline='\n')
    os.chmod(aot, 0o700)
    monkeypatch.setattr(sys, 'executable', str(newer / 'python'))
    again = invoke('--state-dir', tmp_path / 'state', '--fake', 'setup', '--label', 'Alpha',
                   '--project-dir', project, '--prepare-only', '--host', 'claude')
    assert read_runtime_pointer(project) == aot
    assert again['connected'] and 'pairing_command' not in again
    assert again['client_id'] == result['client_id']


@needs_system_python
def test_relay_brings_the_tools_up_when_setup_finishes_mid_session(tmp_path):
    project = tmp_path / 'project-claude'
    project.mkdir(mode=0o700)
    session = RelaySession(project)
    try:
        session.send(1, 'initialize', protocolVersion='2025-06-18', capabilities={},
                     clientInfo={'name': 'test-host', 'version': '1'})
        init = session.receive()['result']
        assert init['protocolVersion'] == '2025-06-18'
        assert init['capabilities']['tools'] == {'listChanged': True}
        assert 'with no restart' in init['instructions']
        session.send(None, 'notifications/initialized')
        assert session.call(2, 'tools/list')['result'] == {'tools': []}
        assert session.call(3, 'ping')['result'] == {}
        error = session.call(4, 'tools/call')['error']
        assert error['code'] == -32603 and 'not set up' in error['message']
        # Setup runs in another process while the session stays open.
        assert connected(tmp_path)['project_dir'] == str(project)
        assert session.receive() == {'jsonrpc': '2.0', 'method': 'notifications/tools/list_changed'}
        tools = {tool['name'] for tool in session.call(5, 'tools/list')['result']['tools']}
        assert {'messages_poll', 'message_send', 'messages_ack'} <= tools
    finally:
        assert session.close() == 0


@needs_system_python
def test_idle_monitor_starts_watching_once_the_folder_is_set_up(tmp_path):
    plain = tmp_path / 'plain'
    plain.mkdir()
    process = subprocess.Popen([SYSTEM_PYTHON, str(LAUNCHER), 'watch', '--idle-if-unconfigured', '--format', 'text'],
                               cwd=plain, env=clean_env(AOT_PROJECT_DIR=str(plain)), stdout=subprocess.PIPE,
                               stderr=subprocess.PIPE, stdin=subprocess.DEVNULL, text=True)
    try:
        time.sleep(1.0)
        assert process.poll() is None
        profile = plain / '.aot-profile.json'
        profile.write_text('{}', encoding='utf-8', newline='\n')
        os.chmod(profile, 0o600)
        write_pointer(plain, {'version': 1, 'executable': str(stub_runtime(tmp_path))})
        out, err = process.communicate(timeout=20)
        assert process.returncode == 0, err
        assert out.splitlines() == [str(plain), 'watch', '--format', 'text']
    finally:
        if process.poll() is None:
            process.kill()


def relay_starts(tmp_path):
    return len((tmp_path / 'pids').read_text(encoding='utf-8').split())


def notices_until(session, seconds):
    import queue
    received, deadline = [], time.monotonic() + seconds
    while time.monotonic() < deadline:
        try:
            received.append(session.lines.get(timeout=max(0.05, deadline - time.monotonic())))
        except queue.Empty:
            break
    return received


@needs_system_python
def test_relay_backs_off_from_a_runtime_that_keeps_crashing(tmp_path):
    project = Path(prepare(tmp_path, 'claude')['project_dir'])
    write_pointer(project, {'version': 1, 'executable': str(fake_mcp_runtime(tmp_path, 'crashy'))})
    session = RelaySession(project)
    try:
        session.send(1, 'initialize', protocolVersion='2025-06-18')
        assert session.receive()['result']['serverInfo']['version'] == 'crashy'
        session.send(None, 'notifications/initialized')
        later = notices_until(session, 8)
        # Once at once, then after 4 s and 8 s: at most three starts in 8 s, not one every 2 s.
        assert relay_starts(tmp_path) <= 3
        assert len(later) <= 2
    finally:
        assert session.close() == 0


@needs_system_python
def test_relay_answers_initialize_itself_when_the_runtime_dies_first(tmp_path):
    project = Path(prepare(tmp_path, 'claude')['project_dir'])
    write_pointer(project, {'version': 1, 'executable': str(fake_mcp_runtime(tmp_path, 'mute'))})
    session = RelaySession(project)
    try:
        session.send(1, 'initialize', protocolVersion='2025-06-18')
        init = session.receive()
        assert init['id'] == 1 and init['result']['serverInfo']['version'] == 'launcher'
        session.send(None, 'notifications/initialized')
        assert session.call(2, 'tools/list')['result'] == {'tools': []}
        write_pointer(project, {'version': 1, 'executable': str(fake_mcp_runtime(tmp_path, 'v1'))})
        assert session.receive() == {'jsonrpc': '2.0', 'method': 'notifications/tools/list_changed'}
        assert session.call(3, 'tools/list')['result']['version'] == 'v1'
    finally:
        assert session.close() == 0


def test_waiting_instructions_carry_the_runtime_guidance():
    import importlib.util
    from agents_over_telegram.mcp_server import INSTRUCTIONS
    spec = importlib.util.spec_from_file_location('aot_host_launch', LAUNCHER)
    launcher = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(launcher)
    assert launcher.RUNTIME_INSTRUCTIONS == INSTRUCTIONS
    assert launcher.WAITING_INSTRUCTIONS.endswith(INSTRUCTIONS)

# ------------------------------------------------------------ inbox tools from a shell

def run_tool(project, name, arguments=None, raw=None):
    stdin = raw if raw is not None else ('' if arguments is None else json.dumps(arguments))
    result = subprocess.run([str(RUNTIME), 'tool', name], input=stdin, capture_output=True, text=True,
                            cwd=project, env=clean_env(), timeout=60)
    return result.returncode, (json.loads(result.stdout) if result.stdout else None), result.stderr


def test_tool_command_handles_the_inbox_like_the_mcp_server(tmp_path):
    result = connected(tmp_path)
    project = Path(result['project_dir'])
    code, status, _ = run_tool(project, 'connection_status')
    assert code == 0 and status['ok'] and status['result']['connected']
    fake_message(result['state_dir'], 'hello from the group', sender=123)
    code, page, _ = run_tool(project, 'messages_poll')
    assert code == 0 and page['result']['message_content_trust'] == 'untrusted'
    [delivery] = page['result']['deliveries']
    assert delivery['text'] == 'hello from the group'
    code, sent, _ = run_tool(project, 'message_send', {'text': 'it\'s "quoted"\nand multi-line',
                                                         'idempotency_key': 'k1', 'reply_to': delivery['message_id']})
    assert code == 0 and sent['result']['status'] == 'sent'
    fake = read_private_json(Path(result['state_dir']) / 'fake-transport.json')
    assert fake['sent'][-1]['text'].endswith('it\'s "quoted"\nand multi-line')
    code, acked, _ = run_tool(project, 'messages_ack', {'delivery_ids': [delivery['delivery_id']],
                                                        'lease_token': page['result']['lease_token']})
    assert code == 0 and acked['result']['acknowledged'] == [delivery['delivery_id']]


def test_tool_command_refuses_bad_input_without_echoing_it(tmp_path):
    project = Path(connected(tmp_path)['project_dir'])
    for name, raw, error in [('no_such_tool', '', 'unknown_tool'), ('message_send', '{not json', 'invalid_arguments'),
                             ('message_send', '["a list"]', 'invalid_arguments'),
                             ('message_send', '{"text": "missing key"}', 'invalid_arguments')]:
        code, body, stderr = run_tool(project, name, raw=raw)
        assert code == 1 and body == {'ok': False, 'error': body['error']} and body['error']['code'] == error
        assert 'not json' not in stderr and 'missing key' not in json.dumps(body)


def test_tool_command_needs_the_project_folder(tmp_path):
    connected(tmp_path)
    elsewhere = tmp_path / 'elsewhere'
    elsewhere.mkdir()
    code, body, stderr = run_tool(elsewhere, 'messages_poll')
    assert (code, body) == (1, None) and 'project_profile_missing_run_setup' in stderr


@pytest.mark.parametrize('host,event,field', [('claude', 'SessionStart', 'additionalContext'),
                                              ('claude', 'Stop', 'reason'),
                                              ('codex', 'SessionStart', 'additionalContext')])
def test_only_claude_hook_notices_name_the_shell_fallback(tmp_path, host, event, field):
    result = connected(tmp_path)
    receive(result)
    answer = json.loads(run_hook(json.dumps({'hook_event_name': event}), '--project-dir',
                                 result['project_dir'], host=host).stdout)
    text = answer['reason'] if field == 'reason' else answer['hookSpecificOutput'][field]
    assert text.startswith('[Agents over Telegram] 1 new Telegram message')
    assert (f'"{RUNTIME}" tool messages_poll' in text) == (host == 'claude')


# ------------------------------------------------------------ following a new runtime

def follow_project(tmp_path):
    """A connected project whose pointer names this checkout's real runtime."""
    result = connected(tmp_path)
    project = Path(result['project_dir'])
    write_pointer(project, {'version': 1, 'executable': str(RUNTIME)})
    return project


@pytest.mark.parametrize('follow', [True, False])
def test_waiter_exits_3_when_the_pointer_names_another_runtime(tmp_path, follow):
    project = follow_project(tmp_path)
    env = clean_env(AOT_FOLLOW_RUNTIME='1') if follow else clean_env()
    process = subprocess.Popen([str(RUNTIME), 'wake', '--host', 'claude', '--project-dir', str(project),
                                '--session', 'follow', '--duration', '8'], stdout=subprocess.PIPE,
                               stderr=subprocess.PIPE, stdin=subprocess.DEVNULL, env=env, text=True)
    try:
        time.sleep(1.0)
        write_pointer(project, {'version': 1, 'executable': str(stub_runtime(tmp_path))})
        # Following: exit 3 at the next cycle. Not following: run to the duration, then 0.
        assert process.wait(timeout=30) == (3 if follow else 0)
        assert process.stderr.read() == ''
    finally:
        if process.poll() is None:
            process.kill()


def test_watch_exits_3_when_following_and_the_pointer_moves(tmp_path):
    project = follow_project(tmp_path)
    process = subprocess.Popen([str(RUNTIME), 'watch', '--format', 'text', '--duration', '8'], cwd=project,
                               stdout=subprocess.PIPE, stderr=subprocess.PIPE, stdin=subprocess.DEVNULL,
                               env=clean_env(AOT_FOLLOW_RUNTIME='1'), text=True)
    try:
        time.sleep(1.0)
        write_pointer(project, {'version': 1, 'executable': str(stub_runtime(tmp_path))})
        assert process.wait(timeout=30) == 3
        assert process.stdout.read() == ''  # no "Watching stopped" line for the host
    finally:
        if process.poll() is None:
            process.kill()


def moving_stub(tmp_path, project, name, then):
    """A runtime that, when followed, points the project at ``then`` and exits 3."""
    path = tmp_path / 'bin' / name
    path.parent.mkdir(exist_ok=True)
    pointer = project / RUNTIME_POINTER
    path.write_text('#!/bin/sh\n[ "$AOT_FOLLOW_RUNTIME" = 1 ] || exit 1\n'
                    f'rm -f "{pointer}"\n'
                    f'printf \'{{"version": 1, "executable": "%s"}}\' "{then}" > "{pointer}"\n'
                    f'chmod 600 "{pointer}"\nexit 3\n', encoding='utf-8', newline='\n')
    os.chmod(path, 0o700)
    return path


@needs_system_python
def test_launcher_restarts_the_waiter_on_the_new_runtime(tmp_path):
    project = Path(prepare(tmp_path, 'claude')['project_dir'])
    newer = wake_stub(tmp_path, 'printf "[Agents over Telegram] from the new runtime" >&2\nexit 2\n')
    write_pointer(project, {'version': 1, 'executable': str(moving_stub(tmp_path, project, 'old', newer))})
    wake = launch(project, 'wake', '--host', 'claude', stdin='{}')
    assert (wake.returncode, wake.stderr) == (2, '[Agents over Telegram] from the new runtime')


@needs_system_python
def test_launcher_stops_when_a_runtime_says_moved_but_the_pointer_did_not(tmp_path):
    project = Path(prepare(tmp_path, 'claude')['project_dir'])
    write_pointer(project, {'version': 1, 'executable': str(wake_stub(tmp_path, 'exit 3\n'))})
    started = time.monotonic()
    wake = launch(project, 'wake', '--host', 'claude', stdin='{}')
    assert (wake.returncode, wake.stderr) == (0, '') and time.monotonic() - started < 10


@needs_system_python
def test_launcher_moves_the_monitor_to_the_new_runtime(tmp_path):
    project = Path(prepare(tmp_path, 'claude')['project_dir'])
    newer = stub_runtime(tmp_path)  # prints its folder and arguments
    write_pointer(project, {'version': 1, 'executable': str(moving_stub(tmp_path, project, 'old', newer))})
    watch = launch(project, 'watch', '--idle-if-unconfigured', '--format', 'text')
    assert watch.returncode == 0, watch.stderr
    assert watch.stdout.splitlines() == [str(project), 'watch', '--format', 'text']


def test_wake_log_is_bounded_and_never_fails(tmp_path):
    from agents_over_telegram import hosts
    state = tmp_path / 'state'
    state.mkdir()
    (state / 'wake.log').write_text('x' * (hosts.WAKE_LOG_LIMIT + 1), encoding='utf-8')
    hosts._wake_log(state, 'k', 'start', 'turn_end')
    assert (state / 'wake.log.1').stat().st_size == hosts.WAKE_LOG_LIMIT + 1
    assert json.loads((state / 'wake.log').read_text(encoding='utf-8'))['reason'] == 'turn_end'
    hosts._wake_log(tmp_path / 'missing', 'k', 'exit', 'woken')
    assert not (tmp_path / 'missing').exists()
