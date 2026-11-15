"""Antigravity host: setup, plugin hooks in Antigravity's own format, and the background waiter.

Offline: fake transport state in tmp_path. The real agy CLI checks are in docs/HOSTS.md.
"""
import json
import os
from pathlib import Path
import subprocess
import sys
import time

import pytest

from agents_over_telegram.hosts import waiter_command, wake_key
from test_hosts import RUNTIME, clean_env, connected, receive, retire, wait_for_owner, wake_log
from test_hosts import isolated_cwd  # noqa: F401 (autouse fixture)

ROOT = Path(__file__).resolve().parents[1]
SESSION = 'ec33ebf9-0cba-4100-8142-c61503f6c587'


def hook(result, event, session=SESSION, **fields):
    payload = {'hookEventName': event, 'conversationId': session, 'cwd': result['project_dir'],
               'workspacePaths': [result['project_dir']], **fields}
    done = subprocess.run([sys.executable, '-m', 'agents_over_telegram', 'hook', '--host', 'antigravity'],
                          input=json.dumps(payload), capture_output=True, text=True, env=clean_env(), timeout=60)
    assert (done.returncode, done.stderr) == (0, '')
    return json.loads(done.stdout) if done.stdout else None


def injected(answer):
    [step] = answer['injectSteps']
    return step['systemMessage']['systemMessage']


def start_waiter(result, session=SESSION):
    return subprocess.Popen([str(RUNTIME), 'wake', '--host', 'antigravity', '--project-dir', result['project_dir'],
                             '--session', session], stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                            stdin=subprocess.DEVNULL, env=clean_env(), text=True)


def plugin(result):
    folder = Path(result['project_dir']) / '.agents' / 'plugins' / 'agents-over-telegram-project'
    return {child.name: json.loads(child.read_text(encoding='utf-8')) for child in folder.iterdir()}


def test_setup_writes_a_project_plugin_with_only_the_scoped_server(tmp_path):
    result = connected(tmp_path, 'antigravity')
    files = plugin(result)
    assert sorted(files) == ['mcp_config.json', 'plugin.json']
    assert files['plugin.json']['name'] == 'agents-over-telegram-project'
    server = files['mcp_config.json']['mcpServers']['telegram']
    assert server['command'] == str(RUNTIME) and server['args'][-2:] == ['--client-file', result['client_file']]
    from agents_over_telegram.onboarding import HOST_NEXT_STEPS
    assert '.agents/plugins/agents-over-telegram-project' in HOST_NEXT_STEPS['antigravity']


def test_the_user_plugin_hooks_answer_through_the_launcher(tmp_path):
    result = connected(tmp_path, 'antigravity')
    receive(result)
    hooks = json.loads((ROOT / 'hooks.json').read_text(encoding='utf-8'))['agents-over-telegram']
    assert set(hooks) == {'SessionStart', 'PreInvocation', 'Stop'}
    [handler] = hooks['PreInvocation']
    # Antigravity runs plugin hooks in the plugin folder through sh -c; the
    # agent may work in a subfolder, so the workspace names the project.
    payload = {'hookEventName': 'PreInvocation', 'conversationId': SESSION,
               'cwd': str(Path(result['project_dir']) / 'src'), 'workspacePaths': [result['project_dir']]}
    done = subprocess.run(['sh', '-c', handler['command']], cwd=ROOT, input=json.dumps(payload),
                          capture_output=True, text=True, env=clean_env(), timeout=60)
    assert done.returncode == 0
    assert injected(json.loads(done.stdout)).startswith('[Agents over Telegram] 1 new Telegram message')


def test_setup_removes_the_project_hooks_that_0_10_8_wrote(tmp_path):
    result = connected(tmp_path, 'antigravity')
    old = Path(result['project_dir']) / '.agents' / 'plugins' / 'agents-over-telegram'
    old.mkdir()
    (old / 'plugin.json').write_text(json.dumps({'name': 'agents-over-telegram', 'description':
                                                 'Written by "aot setup --host antigravity"; run setup again.'}))
    (old / 'hooks.json').write_text('{}')
    (old / 'mcp_config.json').write_text('{}')
    other = Path(result['project_dir']) / '.agents' / 'plugins' / 'other'
    other.mkdir()
    (other / 'plugin.json').write_text('{"name": "other"}')
    from test_hosts import prepare
    prepare(tmp_path, 'antigravity')
    assert not old.exists() and other.exists()


def test_a_folder_that_only_looks_like_the_old_plugin_is_kept(tmp_path):
    result = connected(tmp_path, 'antigravity')
    old = Path(result['project_dir']) / '.agents' / 'plugins' / 'agents-over-telegram'
    old.mkdir()
    (old / 'plugin.json').write_text('{"name": "agents-over-telegram", "description": "mine"}')
    (old / 'hooks.json').write_text('{}')
    (old / 'mcp_config.json').write_text('{}')
    from test_hosts import prepare
    prepare(tmp_path, 'antigravity')
    assert old.exists()


def test_setup_again_updates_the_plugin_and_keeps_other_plugins(tmp_path):
    result = connected(tmp_path, 'antigravity')
    folder = Path(result['project_dir']) / '.agents' / 'plugins'
    other = folder / 'other' / 'plugin.json'
    other.parent.mkdir()
    other.write_text('{"name": "other"}\n', encoding='utf-8')
    servers = folder / 'agents-over-telegram-project' / 'mcp_config.json'
    servers.write_text('{}\n', encoding='utf-8')
    from test_hosts import prepare
    prepare(tmp_path, 'antigravity')
    assert 'telegram' in json.loads(servers.read_text(encoding='utf-8'))['mcpServers']
    assert other.read_text(encoding='utf-8') == '{"name": "other"}\n'


def test_the_repository_is_an_antigravity_plugin_with_the_skills():
    from agents_over_telegram import __version__
    manifest = json.loads((ROOT / '.antigravity-plugin' / 'plugin.json').read_text(encoding='utf-8'))
    assert manifest['name'] == 'agents-over-telegram' and manifest['version'] == __version__
    # Pi installs the repository through its root package.json.
    package = json.loads((ROOT / 'package.json').read_text(encoding='utf-8'))
    assert package['version'] == __version__ and package['pi']['skills'] == ['./skills']
    assert all((ROOT / path).is_file() for path in package['pi']['extensions'])
    # Native plugins load hooks.json from the plugin root and nothing else here:
    # the MCP server comes from each project's own plugin.
    assert not (ROOT / 'mcp_config.json').exists()
    assert {path.parent.name for path in (ROOT / 'skills').glob('*/SKILL.md')} == {'telegram-setup', 'telegram-coordinate'}


def test_session_start_names_pending_work_and_the_exact_waiter(tmp_path):
    result = connected(tmp_path, 'antigravity')
    receive(result, 'secret message body')
    text = injected(hook(result, 'SessionStart'))
    assert text.startswith('[Agents over Telegram] 1 new Telegram message')
    assert waiter_command(Path(result['project_dir']).resolve(), SESSION) in text
    assert 'secret message body' not in text


def test_pre_invocation_announces_each_new_message_once(tmp_path):
    result = connected(tmp_path, 'antigravity')
    assert hook(result, 'PreInvocation', invocationNum=0) is None
    receive(result, 'secret message body')
    text = injected(hook(result, 'PreInvocation', invocationNum=1))
    assert text.startswith('[Agents over Telegram] 1 new Telegram message') and 'secret' not in text
    # Before every model call: the same message is never announced twice, and
    # the waiter is not mentioned on every call.
    assert hook(result, 'PreInvocation', invocationNum=2) is None


def test_stop_asks_for_the_waiter_once_per_prompt_and_never_loops(tmp_path):
    result = connected(tmp_path, 'antigravity')
    first = hook(result, 'Stop', lastUserInput='do the thing', fullyIdle=True)
    assert first['decision'] == 'continue' and waiter_command(Path(result['project_dir']).resolve(), SESSION) in first['reason']
    # Antigravity 1.5.9 never sets continuationActive; AOT must not ask again by itself.
    assert hook(result, 'Stop', lastUserInput='do the thing', fullyIdle=True, continuationActive=False) is None
    # A new prompt may ask again.
    assert hook(result, 'Stop', lastUserInput='next task', fullyIdle=True)['decision'] == 'continue'
    # New messages are always announced, each once.
    receive(result)
    news = hook(result, 'Stop', lastUserInput='next task')
    assert news['decision'] == 'continue' and news['reason'].startswith('[Agents over Telegram] 1 new Telegram message')
    assert hook(result, 'Stop', lastUserInput='next task') is None


def test_a_running_waiter_is_not_asked_for_again(tmp_path):
    result = connected(tmp_path, 'antigravity')
    waiter = start_waiter(result)
    try:
        wait_for_owner(result, SESSION, waiter)
        deadline = time.monotonic() + 30
        while hook(result, 'Stop', lastUserInput='a', fullyIdle=False) is not None:
            # The lock is taken just after the owner file is written.
            assert time.monotonic() < deadline
            time.sleep(0.2)
        assert hook(result, 'SessionStart') is None
    finally:
        retire(result, SESSION, waiter)


def test_stop_asks_again_after_the_waiter_ran_for_the_same_prompt(tmp_path):
    result = connected(tmp_path, 'antigravity')
    assert hook(result, 'Stop', lastUserInput='p')['decision'] == 'continue'
    assert hook(result, 'Stop', lastUserInput='p') is None
    # The agent started a waiter, which later woke it and ended.
    time.sleep(0.05)
    waiter = start_waiter(result)
    wait_for_owner(result, SESSION, waiter)
    retire(result, SESSION, waiter)
    again = hook(result, 'Stop', lastUserInput='p')
    assert again is not None and again['decision'] == 'continue'


def test_waiter_prints_the_notice_and_how_to_restart_it(tmp_path):
    result = connected(tmp_path, 'antigravity')
    # Waiting work is announced at once. Collecting from the test while the
    # waiter also collects made this test time out on a slow CI runner.
    receive(result, 'secret message body')
    waiter = start_waiter(result)
    try:
        out, err = waiter.communicate(timeout=60)
    finally:
        if waiter.poll() is None:
            waiter.kill()
    # Antigravity wakes the agent when the command ends and shows its output.
    assert (waiter.returncode, err) == (0, '')
    assert out.startswith('[Agents over Telegram] 1 new Telegram message')
    assert waiter_command(Path(result['project_dir']).resolve(), SESSION) in out
    assert 'secret message body' not in out
    assert [line['reason'] for line in wake_log(result) if line['event'] == 'exit'] == ['woken']


@pytest.mark.parametrize('session', ['', 'not a session', 'x' * 129])
def test_waiter_without_a_usable_session_does_nothing(tmp_path, session):
    result = connected(tmp_path, 'antigravity')
    done = subprocess.run([str(RUNTIME), 'wake', '--host', 'antigravity', '--project-dir', result['project_dir'],
                           '--session', session], capture_output=True, text=True, env=clean_env(), timeout=60,
                          stdin=subprocess.DEVNULL)
    assert (done.returncode, done.stdout, done.stderr) == (0, '', '')


def test_hooks_are_silent_in_unconfigured_folders(tmp_path):
    folder = tmp_path / 'other'
    folder.mkdir()
    for event in ('SessionStart', 'PreInvocation', 'Stop', 'PreToolUse'):
        assert hook({'project_dir': str(folder)}, event) is None
