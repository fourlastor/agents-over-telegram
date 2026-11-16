#!/usr/bin/env python3
"""Check that a release tag matches the version in every version file.

The release workflow runs this before it publishes anything:

    python3 scripts/release_check.py v0.11.0
"""
import json
from pathlib import Path
import re
import sys
import tomllib

ROOT = Path(__file__).resolve().parents[1]
JSON_FILES = ('package.json', 'integrations/pi/package.json', '.claude-plugin/plugin.json',
              '.codex-plugin/plugin.json', '.antigravity-plugin/plugin.json')


def versions(root=ROOT):
    """Return {file: version} for every file that carries the release version."""
    found = {name: json.loads((root / name).read_text(encoding='utf-8'))['version'] for name in JSON_FILES}
    found['pyproject.toml'] = tomllib.loads((root / 'pyproject.toml').read_text(encoding='utf-8'))['project']['version']
    init = (root / 'src' / 'agents_over_telegram' / '__init__.py').read_text(encoding='utf-8')
    match = re.search(r'^__version__ = "([^"]+)"$', init, re.MULTILINE)
    found['src/agents_over_telegram/__init__.py'] = match.group(1) if match else None
    lock = tomllib.loads((root / 'uv.lock').read_text(encoding='utf-8'))
    found['uv.lock'] = next((item.get('version') for item in lock.get('package', [])
                             if item.get('name') == 'agents-over-telegram'), None)
    readme = re.search(r'^Version (\S+) supports ', (root / 'README.md').read_text(encoding='utf-8'), re.MULTILINE)
    found['README.md'] = readme.group(1) if readme else None
    return found


def problems(tag, root=ROOT):
    """Return a list of mismatches between the tag and the version files."""
    match = re.fullmatch(r'v(\d+\.\d+\.\d+)', tag)
    if not match:
        return [f'tag {tag!r} is not of the form vX.Y.Z']
    return [f'{name} has version {version!r}, the tag says {match.group(1)!r}'
            for name, version in versions(root).items() if version != match.group(1)]


if __name__ == '__main__':
    if len(sys.argv) != 2:
        sys.exit('usage: release_check.py vX.Y.Z')
    found = problems(sys.argv[1])
    for line in found:
        print(line, file=sys.stderr)
    sys.exit(1 if found else 0)
