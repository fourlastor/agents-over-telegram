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
        current_entry = servers[SERVER]
        if current_entry == expected:
            return path, raw, raw
        # The same client on another runtime (an update) moves to this one;
        # anything else is someone else's entry.
        if not (isinstance(current_entry, dict) and {**current_entry, 'command': expected['command']} == expected
                and isinstance(current_entry.get('command'), str) and Path(current_entry['command']).is_absolute()):
            raise CredentialError('project_mcp_config_conflict')
    proposed = dict(current)
    proposed[key] = {**servers, SERVER: expected}
    return path, raw, json.dumps(proposed, indent=2) + '\n'


# Antigravity (the agy CLI and the desktop app) loads plugins from the
# project's .agents/plugins/<name>/ folder. Its hooks and skills come from the
# Agents over Telegram plugin installed once for the user (the repository root,
# through .antigravity-plugin/plugin.json). That plugin's MCP server cannot
# tell which project it serves, so setup writes a small project plugin with
# only the scoped telegram server, naming this project's runtime and client
# like the Codex, Pi and Amp entries. A rerun of setup replaces its files.
ANTIGRAVITY_PLUGIN = Path('.agents') / 'plugins' / 'agents-over-telegram-project'
# 0.10.8 wrote the hooks into the project as well, under this name. With the
# user plugin installed they would run twice, so setup removes that folder.
ANTIGRAVITY_OLD_PLUGIN = Path('.agents') / 'plugins' / 'agents-over-telegram'
ANTIGRAVITY_OLD_FILES = ('plugin.json', 'hooks.json', 'mcp_config.json')


def antigravity_plugin_files(project: Path, entry: dict) -> dict:
    """The project plugin's files, relative path -> text."""
    manifest = {'name': 'agents-over-telegram-project', 'displayName': 'Agents over Telegram (this project)',
                'description': "This project's Telegram tools. Written by \"aot setup --host antigravity\"; "
                               'run setup again to update it.',
                'homepage': 'https://github.com/fourlastor/agents-over-telegram', 'license': 'Apache-2.0'}
    servers = {'mcpServers': {SERVER: expected_entry('antigravity', entry)}}
    dump = lambda value: json.dumps(value, indent=2) + '\n'
    return {ANTIGRAVITY_PLUGIN / 'plugin.json': dump(manifest),
            ANTIGRAVITY_PLUGIN / 'mcp_config.json': dump(servers)}


def antigravity_plugin_plan(project: Path, entry: dict):
    """Return [(path, current_text, proposed_text)] without writing anything."""
    from .onboarding import _check_directory, _read_local
    _check_directory(project)
    folder = project
    for part in ANTIGRAVITY_PLUGIN.parts:
        folder = folder / part
        _check_directory(folder)
    plan = []
    for relative, text in antigravity_plugin_files(project, entry).items():
        path = project / relative
        current = _read_local(path) if path.exists() or path.is_symlink() else None
        plan.append((path, current, text))
    return plan


def remove_old_antigravity_plugin(project: Path) -> bool:
    """Remove the project plugin 0.10.8 wrote, only when it is exactly that."""
    folder = project / ANTIGRAVITY_OLD_PLUGIN
    try:
        if folder.is_symlink() or not folder.is_dir():
            return False
        if sorted(child.name for child in folder.iterdir()) != sorted(ANTIGRAVITY_OLD_FILES):
            return False
        manifest = json.loads((folder / 'plugin.json').read_text(encoding='utf-8'))
        if manifest.get('name') != 'agents-over-telegram' or '--host antigravity' not in manifest.get('description', ''):
            return False
        for name in ANTIGRAVITY_OLD_FILES:
            (folder / name).unlink()
        folder.rmdir()
        return True
    except (OSError, ValueError, AttributeError):
        return False
