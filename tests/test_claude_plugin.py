"""Claude Code plugin packaging: manifest, hooks, monitor and marketplace."""
import json
from pathlib import Path
import tomllib

ROOT = Path(__file__).resolve().parents[1]
LAUNCH = '"${CLAUDE_PLUGIN_ROOT}/scripts/aot_host_launch.py"'


def load(relative):
    return json.loads((ROOT / relative).read_text())


def test_manifest_identity_matches_the_other_packages():
    manifest = load('.claude-plugin/plugin.json')
    version = tomllib.loads((ROOT / 'pyproject.toml').read_text())['project']['version']
    assert manifest['name'] == load('.codex-plugin/plugin.json')['name'] == 'agents-over-telegram'
    assert manifest['version'] == load('.codex-plugin/plugin.json')['version'] == version


def test_mcp_server_starts_through_the_project_launcher():
    server = load('.claude-plugin/plugin.json')['mcpServers']['telegram']
    assert server['command'] == 'python3'
    assert server['args'] == ['${CLAUDE_PLUGIN_ROOT}/scripts/aot_host_launch.py', 'mcp']
    assert server['env'] == {'AOT_PROJECT_DIR': '${CLAUDE_PROJECT_DIR}'}
    # A root .mcp.json would also be read by Codex as a bundled server.
    assert not (ROOT / '.mcp.json').exists() and not (ROOT / 'mcp.json').exists()


def test_hooks_cover_tool_boundaries_prompts_and_turn_end():
    manifest = load('.claude-plugin/plugin.json')
    assert manifest['hooks'] == './claude/hooks.json'
    hooks = load('claude/hooks.json')['hooks']
    assert set(hooks) == {'SessionStart', 'UserPromptSubmit', 'PostToolUse', 'Stop'}
    hook = {'type': 'command', 'command': f'python3 {LAUNCH} hook --host claude', 'timeout': 10}
    # asyncRewake wakes an idle session on exit status 2, also where monitors do not start.
    wake = {'type': 'command', 'command': f'python3 {LAUNCH} wake --host claude', 'asyncRewake': True,
            'timeout': 86400}
    for event, entries in hooks.items():
        assert len(entries) == 1 and 'matcher' not in entries[0]
        expected = [hook, wake] if event in ('SessionStart', 'Stop') else [hook]
        assert entries[0]['hooks'] == expected
    # Claude hooks live outside hooks/hooks.json so Codex never loads them.
    assert not (ROOT / 'hooks' / 'hooks.json').exists()


def test_monitor_collects_and_idles_in_unconfigured_projects():
    manifest = load('.claude-plugin/plugin.json')
    assert manifest['experimental']['monitors'] == './claude/monitors.json'
    (monitor,) = load('claude/monitors.json')
    assert monitor['name'] == 'telegram-inbox'
    assert monitor['command'] == f'python3 {LAUNCH} watch --idle-if-unconfigured --collect --format text --host claude'
    assert not (ROOT / 'monitors' / 'monitors.json').exists()


def test_marketplace_lists_this_plugin_from_the_repository_root():
    market = load('.claude-plugin/marketplace.json')
    assert market['name'] == 'agents-over-telegram'
    assert market['plugins'] == [{'name': 'agents-over-telegram', 'source': './',
                                  'description': market['plugins'][0]['description']}]
