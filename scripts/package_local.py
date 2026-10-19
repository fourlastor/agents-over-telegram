#!/usr/bin/env python3
"""Build a source plugin ZIP from an explicit allowlist, never local state."""
import argparse
from pathlib import Path
import zipfile


def package(root, output):
    selected = []
    for name in ("src", "tests", "skills", "scripts", "docs", ".codex-plugin", ".agents/plugins/marketplace.json", "plugin.json", "mcp.json",
                 ".mcp.json", "pyproject.toml", "uv.lock", "README.md", "PROPOSAL.md", "HANDOFF.md"):
        path = root/name
        candidates = path.rglob("*") if path.is_dir() else [path]
        for file in candidates:
            if file.is_file() and not file.is_symlink() and "__pycache__" not in file.parts and file.suffix != ".pyc":
                selected.append(file)
    forbidden = {"credentials.json", "fake-transport.json", "state.sqlite3", "mode.json"}
    assert not any(p.name in forbidden for p in selected)
    output.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(output, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for path in sorted(selected):
            archive.write(path, path.relative_to(root))
    return len(selected)


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("output", type=Path)
    args = parser.parse_args()
    count = package(Path(__file__).resolve().parents[1], args.output.absolute())
    print(f"Packaged {count} source files; no local state or runtime environment included.")
