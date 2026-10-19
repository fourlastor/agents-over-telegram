#!/usr/bin/env python3
"""Validate a local package using vendored upstream schemas, without networking."""
from __future__ import annotations

import argparse
import hashlib
from importlib.util import module_from_spec, spec_from_file_location
import json
from pathlib import Path

from jsonschema import Draft202012Validator


def validate_marketplace(root: Path, plugin_name: str) -> None:
    """Check this repository's optional, single-plugin distribution catalog."""
    path = root / ".agents" / "plugins" / "marketplace.json"
    if not path.exists():
        # Private per-profile packages are installable without a marketplace.
        return
    if not path.resolve().is_relative_to(root):
        raise ValueError("Marketplace manifest escapes the package")
    marketplace = json.loads(path.read_text())
    if marketplace.get("name") != "agents-over-telegram":
        raise ValueError("Marketplace name must be agents-over-telegram")
    if marketplace.get("interface", {}).get("displayName") != "Agents over Telegram":
        raise ValueError("Marketplace display name is missing or invalid")
    entries = marketplace.get("plugins")
    if not isinstance(entries, list) or len(entries) != 1 or not isinstance(entries[0], dict):
        raise ValueError("Marketplace must contain exactly one plugin")
    entry = entries[0]
    if entry.get("name") != plugin_name:
        raise ValueError("Marketplace and plugin identity differ")
    if entry.get("source") != {"source": "local", "path": "./"}:
        raise ValueError("Marketplace source must point to the repository root with ./")
    if entry.get("policy") != {"installation": "AVAILABLE", "authentication": "ON_INSTALL"}:
        raise ValueError("Marketplace must allow user-controlled installation")


def validate_package(root: Path, schema_root: Path | None = None) -> None:
    root = root.resolve()
    schema_root = schema_root or Path(__file__).resolve().parents[1] / "docs" / "schemas"
    sources = json.loads((schema_root / "sources.json").read_text())
    data = {}
    for kind in ("plugin", "mcp"):
        schema_bytes = (schema_root / f"{kind}.schema.json").read_bytes()
        if hashlib.sha256(schema_bytes).hexdigest() != sources["schemas"][kind]["sha256"]:
            raise ValueError(f"Vendored {kind} schema checksum mismatch")
        schema = json.loads(schema_bytes)
        Draft202012Validator.check_schema(schema)
        path = root / f"{kind}.json"
        if kind == "mcp" and not path.exists():
            continue
        data[kind] = json.loads(path.read_text())
        Draft202012Validator(schema).validate(data[kind])
    validate_marketplace(root, data["plugin"]["name"])
    compat = json.loads((root / ".codex-plugin" / "plugin.json").read_text())
    for key in ("name", "version", "description"):
        if compat.get(key) != data["plugin"].get(key):
            raise ValueError(f"Portable and compatibility identity differ: {key}")
    if compat.get("skills") != "./skills/":
        raise ValueError("Compatibility component paths are invalid")
    legacy_path = root / ".mcp.json"
    if "mcp" in data:
        if compat.get("mcpServers") != "./.mcp.json" or not legacy_path.is_file():
            raise ValueError("Bundled MCP requires both configurations and a compatibility reference")
        legacy_mcp = json.loads(legacy_path.read_text())
        if legacy_mcp != {"mcpServers": data["mcp"]["mcpServers"]}:
            raise ValueError("Portable and compatibility MCP configurations differ")
    elif legacy_path.exists() or "mcpServers" in compat:
        raise ValueError("Skills-only packages must not declare compatibility MCP configuration")
    if set(p.name for p in (root / ".codex-plugin").iterdir()) != {"plugin.json"}:
        raise ValueError("Only plugin.json belongs in the compatibility directory")
    for server in data.get("mcp", {}).get("mcpServers", {}).values():
        if server["type"] != "stdio":
            raise ValueError("This MVP only packages local stdio servers")
        for value in [server.get("cwd", ""), *server.get("args", [])]:
            if value.startswith("${PLUGIN_ROOT}/"):
                relative = Path(value.removeprefix("${PLUGIN_ROOT}/"))
                target = (root / relative).resolve()
                if not target.is_relative_to(root) or not target.exists():
                    raise ValueError("A plugin-relative server path is missing or escapes the package")
    onboarding = data["plugin"].get("extensions", {}).get("com.openai", {}).get("onboardingSkill")
    compatibility_onboarding = compat.get("extensions", {}).get("com.openai", {}).get("onboardingSkill")
    if onboarding != compatibility_onboarding:
        raise ValueError("Portable and compatibility onboarding skills differ")
    if onboarding is not None:
        if not isinstance(onboarding, str) or not onboarding.startswith("./"):
            raise ValueError("Onboarding skill must use a plugin-relative ./ path")
        relative = Path(onboarding[2:])
        if relative.parts[:1] != ("skills",) or len(relative.parts) != 3 or relative.name != "SKILL.md" or ".." in relative.parts:
            raise ValueError("Onboarding skill must point to a packaged skills/<name>/SKILL.md")
        target = (root / relative).resolve()
        if not target.is_relative_to(root / "skills") or not target.is_file():
            raise ValueError("Onboarding skill is missing or escapes the package")
    skills = list((root / "skills").glob("*/SKILL.md"))
    if not skills:
        raise ValueError("No packaged skills found")
    for skill in skills:
        text = skill.read_text()
        if not text.startswith("---\n"):
            raise ValueError("Skill frontmatter is missing")
        header = text.split("---", 2)[1]
        if "name:" not in header or "description:" not in header:
            raise ValueError("Skill frontmatter is incomplete")
    if onboarding is not None:
        # Private profiles reuse an already configured runtime and have no onboarding.
        # The distributable plugin must carry both installer-independent lock exports.
        spec = spec_from_file_location("export_requirements", Path(__file__).with_name("export_requirements.py"))
        exports = module_from_spec(spec)
        spec.loader.exec_module(exports)
        exports.validate_exports(root)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("root", nargs="?", type=Path, default=Path(__file__).resolve().parents[1])
    args = parser.parse_args()
    validate_package(args.root)
    print("Package valid: schemas, compatibility, marketplace, component paths and locked runtime requirements")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
