#!/usr/bin/env python3
"""Validate a local Codex plugin package offline, using a vendored MCP schema."""
from __future__ import annotations

import argparse
import hashlib
from importlib.util import module_from_spec, spec_from_file_location
import json
import re
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
    schema_bytes = (schema_root / "mcp.schema.json").read_bytes()
    if hashlib.sha256(schema_bytes).hexdigest() != sources["schemas"]["mcp"]["sha256"]:
        raise ValueError("Vendored mcp schema checksum mismatch")
    mcp_schema = json.loads(schema_bytes)
    Draft202012Validator.check_schema(mcp_schema)
    # Codex treats a root Agent Plugins plugin.json as the manifest and then
    # loads no plugin hooks, so .codex-plugin/plugin.json is the only manifest.
    if (root / "plugin.json").exists():
        raise ValueError("A root plugin.json hides .codex-plugin/plugin.json and its hooks from Codex")
    if (root / "mcp.json").exists():
        raise ValueError("A root mcp.json is not read by Codex; bundled MCP uses .mcp.json")
    manifest = json.loads((root / ".codex-plugin" / "plugin.json").read_text())
    name = manifest.get("name")
    if not isinstance(name, str) or not re.fullmatch(r"[a-z0-9]+(?:-[a-z0-9]+)*", name) or len(name) > 64:
        raise ValueError("Plugin name is missing or invalid")
    for key in ("version", "description"):
        if not isinstance(manifest.get(key), str) or not manifest[key].strip():
            raise ValueError(f"Plugin {key} is missing")
    validate_marketplace(root, name)
    if manifest.get("skills") != "./skills/":
        raise ValueError("Plugin skills path is invalid")
    hooks = manifest.get("hooks")
    if hooks is not None:
        if not isinstance(hooks, str) or not hooks.startswith("./"):
            raise ValueError("Plugin hooks must use a plugin-relative ./ path")
        target = (root / hooks[2:]).resolve()
        if not target.is_relative_to(root) or not target.is_file():
            raise ValueError("Plugin hooks file is missing or escapes the package")
    mcp_path = root / ".mcp.json"
    data = {}
    if "mcpServers" in manifest:
        if manifest["mcpServers"] != "./.mcp.json" or not mcp_path.is_file():
            raise ValueError("Bundled MCP requires .mcp.json and a ./.mcp.json reference")
        data["mcp"] = json.loads(mcp_path.read_text())
        if set(data["mcp"]) != {"mcpServers"}:
            raise ValueError("Bundled MCP configuration must contain only mcpServers")
        # .mcp.json has no $schema; the server entries follow the vendored MCP schema.
        Draft202012Validator(mcp_schema).validate({"$schema": mcp_schema["$id"], **data["mcp"]})
    elif mcp_path.exists():
        raise ValueError("Skills-only packages must not declare MCP configuration")
    if set(p.name for p in (root / ".codex-plugin").iterdir()) != {"plugin.json"}:
        raise ValueError("Only plugin.json belongs in the .codex-plugin directory")
    for server in data.get("mcp", {}).get("mcpServers", {}).values():
        if server["type"] != "stdio":
            raise ValueError("This MVP only packages local stdio servers")
        for value in [server.get("cwd", ""), *server.get("args", [])]:
            if value.startswith("${PLUGIN_ROOT}/"):
                relative = Path(value.removeprefix("${PLUGIN_ROOT}/"))
                target = (root / relative).resolve()
                if not target.is_relative_to(root) or not target.exists():
                    raise ValueError("A plugin-relative server path is missing or escapes the package")
    onboarding = manifest.get("extensions", {}).get("com.openai", {}).get("onboardingSkill")
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
    print("Package valid: manifest, marketplace, component paths and locked runtime requirements")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
