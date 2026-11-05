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

from agents_over_telegram import cli
from agents_over_telegram.credentials import read_private_json, write_private_json
from agents_over_telegram.hosts import RUNTIME_POINTER, read_runtime_pointer, runtime_pointer

ROOT = Path(__file__).resolve().parents[1]
LAUNCHER = ROOT / 'scripts' / 'aot_host_launch.py'
SYSTEM_PYTHON = '/usr/bin/python3'
RUNTIME = Path(sys.executable).absolute().parent / 'aot'
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
    value = json.loads(path.read_text())
    servers = value['mcpServers' if host == 'pi' else 'amp.mcpServers']
    return servers['telegram']['args'][-2] == '--client-file' and path.stat().st_mode & 0o077 == 0


@pytest.mark.parametrize('host', HOSTS)
def test_setup_host_writes_private_runtime_pointer(tmp_path, host):
    result = prepare(tmp_path, host)
    project = Path(result['project_dir'])
    pointer = project / RUNTIME_POINTER
    assert result['host'] == host
    assert result['runtime_pointer'] == str(pointer)
    assert json.loads(pointer.read_text()) == {'version': 1, 'executable': str(RUNTIME)}
    assert pointer.read_text() == runtime_pointer(RUNTIME)
    assert pointer.stat().st_mode & 0o077 == 0
    assert read_runtime_pointer(project) == RUNTIME
    codex_config = project / '.codex' / 'config.toml'
    if host == 'codex':
        assert result['config_file'] == str(codex_config)
        assert '[mcp_servers.telegram]' in codex_config.read_text()
    else:
        assert not (project / '.codex').exists()
        expected = {'claude': None, 'pi': project / '.pi' / 'mcp.json',
                    'amp': project / '.amp' / 'settings.json'}[host]
        assert result['config_file'] == (str(expected) if expected else None)
        if expected is not None:
            assert secret_free_json_has_telegram(expected, host)
    secret = read_private_json(Path(result['client_file']))['client_secret']
    assert secret not in pointer.read_text()
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


def test_setup_host_replaces_group_writable_pointer_and_refuses_world_writable(tmp_path):
    result = prepare(tmp_path, 'claude')
    pointer = Path(result['project_dir']) / RUNTIME_POINTER
    pointer.write_text(json.dumps({'version': 1, 'executable': '/elsewhere/aot'}))
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
    (tmp_path / RUNTIME_POINTER).write_text(json.dumps(content))
    os.chmod(tmp_path / RUNTIME_POINTER, 0o600)
    assert read_runtime_pointer(tmp_path) is None


def test_read_runtime_pointer_rejects_symlinks_and_missing(tmp_path):
    assert read_runtime_pointer(tmp_path) is None
    target = tmp_path / 'target.json'
    target.write_text(runtime_pointer(RUNTIME))
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
    path.write_text(content if isinstance(content, str) else json.dumps(content))
    os.chmod(path, mode)
    return path


def stub_runtime(tmp_path, mode=0o700):
    """An executable that records its arguments and working folder."""
    path = tmp_path / 'bin' / 'aot-stub'
    path.parent.mkdir(exist_ok=True)
    path.write_text('#!/bin/sh\nprintf "%s\\n" "$PWD" "$@"\n')
    os.chmod(path, mode)
    return path


@needs_system_python
def test_launcher_without_pointer(tmp_path):
    plain = tmp_path / 'plain'
    plain.mkdir()
    mcp = launch(plain, 'mcp')
    assert mcp.returncode == 2 and 'not set up for this project folder' in mcp.stderr
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
    assert launch(plain, 'mcp', '--idle-if-unconfigured').returncode == 2
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
    assert nested.returncode == 2 and 'not set up' in nested.stderr


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
    assert mcp.returncode == 2
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
    broken.write_text('#!/nonexistent/interpreter\n')
    os.chmod(broken, 0o700)
    write_pointer(project, {'version': 1, 'executable': str(broken)})
    mcp = launch(project, 'mcp')
    assert mcp.returncode == 2 and 'could not be started' in mcp.stderr
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
    manifest = json.loads((output / '.codex-plugin' / 'plugin.json').read_text())
    assert 'hooks' not in manifest
    assert json.loads((ROOT / '.codex-plugin' / 'plugin.json').read_text())['hooks'] == './codex/hooks.json'


# ----------------------------------------------------------------- aot wake

def run_wake(*args, cwd=None, timeout=60):
    return subprocess.run([str(RUNTIME), 'wake', '--host', 'claude', *map(str, args)], capture_output=True,
                          text=True, cwd=cwd, env=clean_env(), timeout=timeout, stdin=subprocess.DEVNULL)


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
        time.sleep(1.0)
        assert first.poll() is None
        # A turn end starts a new waiter; the running one hands over and exits quietly.
        second = start_wake(project, 'abc')
        out, err = first.communicate(timeout=30)
        assert (first.returncode, out, err) == (0, '', '')
        time.sleep(1.0)
        assert second.poll() is None
        # Another session keeps its own waiter.
        assert other.poll() is None
        # Both sessions share a client; only one waiter may see the next message.
        other.kill()
        other.communicate(timeout=10)
        fake_message(result['state_dir'], 'after handover')
        out, err = second.communicate(timeout=30)
        assert second.returncode == 2 and '1 new Telegram message' in err
    finally:
        for process in (first, second, other):
            if process is not None and process.poll() is None:
                process.kill()
                process.communicate(timeout=10)


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
    path.write_text('#!/bin/sh\n' + body)
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
    import fcntl
    import hashlib
    result = connected(tmp_path)
    key = hashlib.sha256(f"{result['client_id']}\0abc".encode()).hexdigest()[:24]
    locks = Path(result['state_dir']) / 'wake-locks'
    locks.mkdir(mode=0o700, exist_ok=True)
    # Stand in for an old waiter that is slow to notice the handover.
    with open(locks / f'{key}.lock', 'a+') as held:
        fcntl.flock(held, fcntl.LOCK_EX)
        waiting = start_wake(result['project_dir'], 'abc')
        try:
            time.sleep(2.0)
            assert waiting.poll() is None
            # A newer owner replaces the waiting one, which then exits quietly.
            (locks / f'{key}.owner').write_text('newer')
            out, err = waiting.communicate(timeout=10)
            assert (waiting.returncode, out, err) == (0, '', '')
        finally:
            if waiting.poll() is None:
                waiting.kill()
                waiting.communicate(timeout=10)
