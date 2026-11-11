"""Project-local MCP configuration for Pi (.pi/mcp.json) and Amp (.amp/settings.json)."""
import json
from pathlib import Path

import pytest

from agents_over_telegram.credentials import CredentialError
from agents_over_telegram.host_config import expected_entry, json_config_plan

ENTRY = {'command': '/opt/aot/bin/aot', 'args': ['--state-dir', '/s', 'mcp', '--client-file', '/s/c.json']}


@pytest.mark.parametrize('host,relative,key', [('pi', '.pi/mcp.json', 'mcpServers'),
                                               ('amp', '.amp/settings.json', 'amp.mcpServers')])
def test_new_file_contains_only_the_scoped_server(tmp_path, host, relative, key):
    path, current, proposed = json_config_plan(tmp_path, host, ENTRY)
    assert path == tmp_path / relative and current is None
    value = json.loads(proposed)
    assert value == {key: {'telegram': expected_entry(host, ENTRY)}}
    assert value[key]['telegram']['command'] == ENTRY['command']


def test_pi_entry_uses_direct_exposure():
    entry = expected_entry('pi', ENTRY)
    assert entry['exposure'] == 'direct' and entry['args'] == ENTRY['args']
    assert 'exposure' not in expected_entry('amp', ENTRY)


@pytest.mark.parametrize('host,relative,key', [('pi', '.pi/mcp.json', 'mcpServers'),
                                               ('amp', '.amp/settings.json', 'amp.mcpServers')])
def test_unrelated_settings_and_servers_are_preserved(tmp_path, host, relative, key):
    path = tmp_path / relative
    path.parent.mkdir(mode=0o700)
    path.write_text(json.dumps({'other.setting': True, key: {'docs': {'command': 'docs-mcp'}}}), encoding='utf-8', newline='\n')
    _, current, proposed = json_config_plan(tmp_path, host, ENTRY)
    value = json.loads(proposed)
    assert value['other.setting'] is True
    assert value[key]['docs'] == {'command': 'docs-mcp'}
    assert value[key]['telegram'] == expected_entry(host, ENTRY)
    assert current == path.read_text(encoding='utf-8')


@pytest.mark.parametrize('host,relative,key', [('pi', '.pi/mcp.json', 'mcpServers'),
                                               ('amp', '.amp/settings.json', 'amp.mcpServers')])
def test_identical_entry_is_a_no_op_and_a_different_one_conflicts(tmp_path, host, relative, key):
    path = tmp_path / relative
    path.parent.mkdir(mode=0o700)
    path.write_text(json.dumps({key: {'telegram': expected_entry(host, ENTRY)}}), encoding='utf-8', newline='\n')
    _, current, proposed = json_config_plan(tmp_path, host, ENTRY)
    assert current == proposed
    path.write_text(json.dumps({key: {'telegram': {'command': '/elsewhere/aot', 'args': []}}}), encoding='utf-8', newline='\n')
    with pytest.raises(CredentialError) as error:
        json_config_plan(tmp_path, host, ENTRY)
    assert error.value.code == 'project_mcp_config_conflict'


@pytest.mark.parametrize('text', ['[]', '{"mcpServers": []}', 'not json'])
def test_invalid_existing_file_is_refused(tmp_path, text):
    path = tmp_path / '.pi' / 'mcp.json'
    path.parent.mkdir(mode=0o700)
    path.write_text(text, encoding='utf-8', newline='\n')
    with pytest.raises(CredentialError) as error:
        json_config_plan(tmp_path, 'pi', ENTRY)
    assert error.value.code == 'invalid_project_config'
