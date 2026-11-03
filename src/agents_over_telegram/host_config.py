"""Project-local MCP configuration for hosts that read JSON settings files.

Codex keeps its TOML entry in onboarding. Pi reads ``.pi/mcp.json`` and Amp
reads ``.amp/settings.json``. Both are written like the Codex entry: unrelated
content is preserved, an existing different ``telegram`` entry is a conflict,
and nothing is changed before the whole plan is checked.
"""
from __future__ import annotations

import json
from pathlib import Path

from .credentials import CredentialError

SERVER = 'telegram'
DESCRIPTION = "This project's scoped Agents over Telegram inbox and replies."

# host -> (relative file, key that holds the server map)
JSON_HOSTS = {
    'pi': (Path('.pi') / 'mcp.json', 'mcpServers'),
    'amp': (Path('.amp') / 'settings.json', 'amp.mcpServers'),
}


def expected_entry(host: str, entry: dict) -> dict:
    """The server entry for one host, built from the shared command and args."""
    value = {'command': entry['command'], 'args': list(entry['args'])}
    if host == 'pi':
        # Direct exposure gives the model plain tool names instead of a code-mode wrapper.
        value.update(exposure='direct', description=DESCRIPTION)
    return value


def json_config_plan(project: Path, host: str, entry: dict):
    """Return (path, current_text, proposed_text) without writing anything."""
    from .onboarding import _check_directory, _read_local
    relative, key = JSON_HOSTS[host]
    folder = project / relative.parent
    _check_directory(project)
    _check_directory(folder)
    path = project / relative
    raw = _read_local(path) if path.exists() or path.is_symlink() else None
    expected = expected_entry(host, entry)
    try:
        current = json.loads(raw) if raw and raw.strip() else {}
        if not isinstance(current, dict):
            raise ValueError()
        servers = current.get(key, {})
        if not isinstance(servers, dict):
            raise ValueError()
    except ValueError:
        raise CredentialError('invalid_project_config') from None
    if SERVER in servers:
        if servers[SERVER] != expected:
            raise CredentialError('project_mcp_config_conflict')
        return path, raw, raw
    proposed = dict(current)
    proposed[key] = {**servers, SERVER: expected}
    return path, raw, json.dumps(proposed, indent=2) + '\n'
