"""Offline proof that AOT reads Codex's plugin hook approval like Codex does."""
import json
import os

from agents_over_telegram import codex_trust
from agents_over_telegram.codex_trust import EVENTS, PLUGIN_ID, plugin_content_hash, status


def make_plugin(home, version='1.0.0'):
    root = home / 'plugins' / 'cache' / 'agents-over-telegram' / 'agents-over-telegram' / version
    (root / '.codex-plugin').mkdir(parents=True)
    (root / '.codex-plugin' / 'plugin.json').write_text(json.dumps({'version': version}))
    (root / 'codex').mkdir()
    (root / 'codex' / 'hooks.json').write_text('{"hooks": {}}')
    return root


def write_config(home, content_hash=None, events=EVENTS, enabled=True):
    lines = [f'[plugins."{PLUGIN_ID}"]', f'enabled = {"true" if enabled else "false"}', '']
    for event in events:
        lines.append(f'[hooks.state."{PLUGIN_ID}:codex/hooks.json:{event}:0:0"]')
        lines.append('trusted_hash = "sha256:declaration"')
        if content_hash:
            lines.append(f'trusted_content_hash = "{content_hash}"')
        lines.append('')
    (home / 'config.toml').write_text('\n'.join(lines))


def test_folder_hash_matches_codex_for_a_fixed_tree(tmp_path):
    # Value computed by this port, which matched Codex's stored hash for a real
    # installed plugin; it guards the byte layout against regressions.
    root = tmp_path / 'p'
    (root / 'a').mkdir(parents=True)
    (root / 'a' / 'x.txt').write_bytes(b'hello')
    (root / 'b.json').write_bytes(b'{}')
    (root / '__pycache__').mkdir()
    (root / '__pycache__' / 'junk.pyc').write_bytes(b'ignored')
    assert plugin_content_hash(root) == GOLDEN
    (root / 'a' / 'cache.pyc').write_bytes(b'also ignored')
    assert plugin_content_hash(root) == GOLDEN
    (root / 'b.json').write_bytes(b'{"changed": 1}')
    assert plugin_content_hash(root) != GOLDEN


def test_status_follows_the_stored_approval(tmp_path):
    home = tmp_path / 'codex'
    assert status(home) == {'status': 'not_installed'}
    root = make_plugin(home)
    write_config(home, content_hash=plugin_content_hash(root))
    assert status(home) == {'plugin_version': '1.0.0', 'status': 'approved'}
    (root / '.codex-plugin' / 'plugin.json').write_text(json.dumps({'version': '1.0.1'}))
    assert status(home)['status'] == 'needs_approval'
    assert status(home)['reason'] == 'plugin_changed_since_approval'


def test_missing_approval_or_disabled_plugin(tmp_path):
    home = tmp_path / 'codex'
    root = make_plugin(home)
    write_config(home, content_hash=plugin_content_hash(root), events=EVENTS[:2])
    assert status(home)['reason'] == 'hook_never_approved'
    write_config(home, enabled=False)
    assert status(home)['status'] == 'not_installed'


def test_newest_installed_version_is_checked(tmp_path):
    home = tmp_path / 'codex'
    old = make_plugin(home, '0.9.9')
    new = make_plugin(home, '0.9.14')
    write_config(home, content_hash=plugin_content_hash(new))
    assert status(home) == {'plugin_version': '0.9.14', 'status': 'approved'}
    assert plugin_content_hash(old) != plugin_content_hash(new)


def test_an_older_approval_uses_the_legacy_baseline(tmp_path):
    home = tmp_path / 'codex'
    root = make_plugin(home)
    write_config(home)
    assert status(home)['status'] == 'approved'  # Codex pins the folder on first use
    baseline = codex_trust._legacy_baseline
    import hashlib, struct
    digest = hashlib.sha256()
    for part in (os.fsencode(str(home / 'config.toml')), PLUGIN_ID.encode()):
        digest.update(struct.pack('<Q', len(part)))
        digest.update(part)
    (home / '.hook-trust').mkdir()
    (home / '.hook-trust' / digest.hexdigest()).write_text('sha256:older')
    assert baseline(home) == 'sha256:older'
    assert status(home)['status'] == 'needs_approval'
    (home / '.hook-trust' / digest.hexdigest()).write_text(plugin_content_hash(root))
    assert status(home)['status'] == 'approved'


def test_project_detection_reads_the_codex_mcp_entry(tmp_path):
    project = tmp_path / 'proj'
    (project / '.codex').mkdir(parents=True)
    client = tmp_path / 'client.json'
    (project / '.codex' / 'config.toml').write_text(
        f'[mcp_servers.telegram]\ncommand = "aot"\nargs = ["mcp", "--client-file", "{client}"]\n')
    assert codex_trust.codex_project_uses(client, project)
    assert codex_trust.codex_project_uses(client, project / 'src' / 'deep')
    assert not codex_trust.codex_project_uses(tmp_path / 'other.json', project)
    assert not codex_trust.codex_project_uses(client, tmp_path / 'elsewhere')
GOLDEN = 'sha256:8ce5d67dfa20e2d6d668ede028a25e5a252604a938af8add37ec4c47f888d2da'


def test_poll_and_instructions_warn_only_for_a_codex_project_needing_approval(tmp_path, monkeypatch):
    from agents_over_telegram import mcp_server
    client = tmp_path / 'client.json'
    monkeypatch.setattr(codex_trust, 'codex_project_uses', lambda path: path == client)
    monkeypatch.setattr(codex_trust, 'status', lambda: {'status': 'needs_approval'})
    monkeypatch.setattr(mcp_server, '_codex_host', lambda: 'cli')
    assert mcp_server._codex_hooks_warning(client) == codex_trust.approve_hint('cli')
    assert mcp_server._codex_hooks_warning(tmp_path / 'other.json') is None
    assert mcp_server._codex_hooks_warning(None) is None
    monkeypatch.setattr(codex_trust, 'status', lambda: {'status': 'approved'})
    assert mcp_server._codex_hooks_warning(client) is None


def test_the_hint_names_the_codex_host_that_started_the_server():
    desktop = {10: (9, '/usr/bin/python3'), 9: (8, '/Applications/ChatGPT.app/Contents/Resources/codex-cli/'
                                                  'CodexCLI.app/Contents/MacOS/codex'),
               8: (1, '/Applications/ChatGPT.app/Contents/MacOS/ChatGPT')}
    cli = {10: (9, '/usr/bin/python3'), 9: (8, '/opt/homebrew/bin/codex'), 8: (1, '/bin/zsh')}
    other = {10: (9, '/usr/bin/python3'), 9: (1, '/Applications/Claude.app/Contents/MacOS/claude')}
    assert codex_trust.codex_host(10, desktop.get) == 'desktop'
    assert codex_trust.codex_host(10, cli.get) == 'cli'
    assert codex_trust.codex_host(10, other.get) is None
    assert '/hooks' in codex_trust.approve_hint('cli') and 'desktop' not in codex_trust.approve_hint('cli')
    assert 'desktop app' in codex_trust.approve_hint('desktop') and '/hooks' not in codex_trust.approve_hint('desktop')
    generic = codex_trust.approve_hint(None)
    assert 'desktop app' in generic and '/hooks' in generic
