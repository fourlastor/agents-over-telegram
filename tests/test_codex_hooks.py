"""Codex hook contract: the plugin's hooks file and `aot hook --host codex`.

These run the real CLI against a fake Telegram transport. Codex sends the
documented stdin fields (cwd, hook_event_name, tool_name, stop_hook_active) and
honors hookSpecificOutput.additionalContext and Stop decision=block.
"""
import json
import os
from pathlib import Path
import subprocess
import sys

import pytest

ROOT = Path(__file__).resolve().parents[1]
AOT = Path(sys.executable).parent / 'aot'
HARNESS = ROOT / 'scripts' / 'e2e_fake.py'


@pytest.fixture
def env(tmp_path):
    root = tmp_path / 'e2e'
    result = subprocess.run([sys.executable, str(HARNESS), 'prepare', '--root', str(root), '--host', 'codex'],
                            capture_output=True, text=True, check=True)
    info = json.loads(result.stdout)
    assert info['connected']
    return root


def inject(root, text):
    subprocess.run([sys.executable, str(HARNESS), 'inject', '--root', str(root), '--text', text],
                   capture_output=True, text=True, check=True)


def hook(root, payload, *extra):
    result = subprocess.run([str(AOT), 'hook', '--host', 'codex', *extra], input=json.dumps(payload),
                            capture_output=True, text=True, cwd=root / 'project', timeout=60)
    assert result.returncode == 0 and result.stderr == ''
    return json.loads(result.stdout) if result.stdout.strip() else None


def payload(root, event, **fields):
    return {'session_id': 's', 'transcript_path': None, 'cwd': str(root / 'project'),
            'hook_event_name': event, 'model': 'test', **fields}


def test_plugin_manifest_points_at_codex_hooks():
    manifest = json.loads((ROOT / '.codex-plugin' / 'plugin.json').read_text())
    assert manifest['hooks'] == './codex/hooks.json'
    hooks = json.loads((ROOT / 'codex' / 'hooks.json').read_text())['hooks']
    assert set(hooks) == {'SessionStart', 'UserPromptSubmit', 'PostToolUse', 'Stop'}
    for entries in hooks.values():
        for entry in entries:
            for item in entry['hooks']:
                assert item['type'] == 'command'
                assert '"${PLUGIN_ROOT}/scripts/aot_host_launch.py" hook --host codex' in item['command']
                assert 'CLAUDE' not in item['command']


def test_codex_hook_collects_itself_and_announces_once(env):
    inject(env, 'Please answer PEACH')
    # Without collection the hook sees nothing: nobody has collected yet.
    assert hook(env, payload(env, 'PostToolUse', tool_name='shell', tool_input={})) is None
    answer = hook(env, payload(env, 'PostToolUse', tool_name='shell', tool_input={}), '--collect-interval', '0.001')
    context = answer['hookSpecificOutput']['additionalContext']
    assert answer['hookSpecificOutput']['hookEventName'] == 'PostToolUse'
    assert '1 new Telegram message' in context and 'PEACH' not in context
    # Announced once; a second tool boundary stays quiet.
    assert hook(env, payload(env, 'PostToolUse', tool_name='shell'), '--collect-interval', '10') is None


def test_codex_hook_collection_is_rate_limited(env):
    hook(env, payload(env, 'PostToolUse', tool_name='shell'), '--collect-interval', '0.001')
    inject(env, 'Second message')
    # The last attempt was just now, so this hook does not contact Telegram again.
    assert hook(env, payload(env, 'PostToolUse', tool_name='shell'), '--collect-interval', '10') is None
    fake = json.loads((env / 'state' / 'fake-transport.json').read_text())
    polls_before = len(fake['poll_offsets'])
    hook(env, payload(env, 'PostToolUse', tool_name='shell'), '--collect-interval', '10')
    fake = json.loads((env / 'state' / 'fake-transport.json').read_text())
    assert len(fake['poll_offsets']) == polls_before


def test_codex_stop_hook_continues_once(env):
    inject(env, 'Late message')
    first = hook(env, payload(env, 'Stop', stop_hook_active=False, last_assistant_message='done'),
                 '--collect-interval', '0.001')
    assert first['decision'] == 'block' and 'messages_poll' in first['reason']
    inject(env, 'Another late message')
    assert hook(env, payload(env, 'Stop', stop_hook_active=True), '--collect-interval', '0.001') is None


def test_codex_hook_uses_payload_cwd_and_stays_silent_elsewhere(env, tmp_path):
    inject(env, 'Hello')
    elsewhere = tmp_path / 'other'
    elsewhere.mkdir()
    result = subprocess.run([str(AOT), 'hook', '--host', 'codex', '--collect-interval', '10'],
                            input=json.dumps({**payload(env, 'UserPromptSubmit'), 'cwd': str(elsewhere)}),
                            capture_output=True, text=True, cwd=elsewhere, timeout=60)
    assert (result.returncode, result.stdout, result.stderr) == (0, '', '')
    # A subfolder is not the project: no parent search.
    sub = env / 'project' / 'src'
    sub.mkdir()
    result = subprocess.run([str(AOT), 'hook', '--host', 'codex', '--collect-interval', '10'],
                            input=json.dumps({**payload(env, 'UserPromptSubmit'), 'cwd': str(sub)}),
                            capture_output=True, text=True, cwd=sub, timeout=60)
    assert (result.returncode, result.stdout) == (0, '')


def test_plugin_launcher_finds_the_project_from_the_hook_input_cwd(env, tmp_path):
    inject(env, 'Hello')
    elsewhere = tmp_path / 'hook-cwd'
    elsewhere.mkdir()
    clean = {k: v for k, v in os.environ.items() if k not in ('AOT_PROJECT_DIR', 'CLAUDE_PROJECT_DIR')}
    result = subprocess.run(['/usr/bin/python3', str(ROOT / 'scripts' / 'aot_host_launch.py'), 'hook', '--host', 'codex',
                             '--collect-interval', '0.001'],
                            input=json.dumps(payload(env, 'PostToolUse', tool_name='shell')),
                            capture_output=True, text=True, cwd=elsewhere, env=clean, timeout=60)
    assert result.returncode == 0 and result.stderr == ''
    answer = json.loads(result.stdout)
    assert '1 new Telegram message' in answer['hookSpecificOutput']['additionalContext']
