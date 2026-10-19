"""Manifest interoperability and launch safety, entirely offline."""
from importlib.util import module_from_spec, spec_from_file_location
from pathlib import Path
import json
import os
import shutil
import subprocess
import sys

import pytest
from jsonschema import ValidationError

ROOT = Path(__file__).resolve().parents[1]


def load_script(name):
    spec = spec_from_file_location(name, ROOT / "scripts" / f"{name}.py")
    module = module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


validator = load_script("validate_package")
launcher = load_script("launch_mcp")


def test_shipped_package_uses_current_schemas():
    validator.validate_package(ROOT)
    assert (ROOT / ".agents/plugins/marketplace.json").is_file()


def test_normal_install_has_no_global_mcp_connection():
    assert not (ROOT / "mcp.json").exists()
    assert not (ROOT / ".mcp.json").exists()
    compat = json.loads((ROOT / ".codex-plugin/plugin.json").read_text())
    assert "mcpServers" not in compat
    assert compat["extensions"]["com.openai"]["onboardingSkill"] == "./skills/telegram-setup/SKILL.md"


def add_bundled_mcp(root):
    servers = {"telegram": {"type": "stdio", "command": "/test/aot", "args": []}}
    (root / "mcp.json").write_text(json.dumps({
        "$schema": "https://agent-plugins.org/schemas/1.0.0/mcp.schema.json", "mcpServers": servers}))
    (root / ".mcp.json").write_text(json.dumps({"mcpServers": servers}))
    path = root / ".codex-plugin/plugin.json"
    compat = json.loads(path.read_text())
    compat["mcpServers"] = "./.mcp.json"
    path.write_text(json.dumps(compat))


def test_manifest_rejects_missing_stdio_type(tmp_path):
    copy_package(tmp_path)
    add_bundled_mcp(tmp_path)
    data = json.loads((tmp_path / "mcp.json").read_text())
    del data["mcpServers"]["telegram"]["type"]
    (tmp_path / "mcp.json").write_text(json.dumps(data))
    with pytest.raises(ValidationError):
        validator.validate_package(tmp_path)


def test_launcher_no_profile_fails_before_starting_any_server():
    process = subprocess.run([sys.executable, str(ROOT / "scripts" / "launch_mcp.py")], env={}, capture_output=True, text=True)
    assert process.returncode == 2
    assert process.stdout == ""
    assert "Configure absolute" in process.stderr


def test_launcher_does_not_echo_invalid_environment():
    fake_secret = "DO_NOT_ECHO_FAKE_PRIVATE_PROFILE"
    with pytest.raises(launcher.ProfileError) as exc:
        launcher.build_command({"AOT_EXECUTABLE": fake_secret})
    assert fake_secret not in str(exc.value)


def test_launcher_uses_one_explicit_profile_without_shell(tmp_path):
    executable = tmp_path / "aot executable"
    executable.write_text("#!/bin/sh\nexit 0\n")
    executable.chmod(0o700)
    state = tmp_path / "private state"
    state.mkdir()
    client = tmp_path / "client; literal.json"
    client.write_text("unused fake capability")
    command = launcher.build_command({"AOT_EXECUTABLE": str(executable), "AOT_STATE_DIR": str(state), "AOT_CLIENT_FILE": str(client)})
    assert command == [str(executable), "--state-dir", str(state), "mcp", "--client-file", str(client)]


def copy_package(tmp_path):
    tmp_path.mkdir(parents=True, exist_ok=True)
    for name in ("plugin.json", ".codex-plugin", ".agents", "skills", "scripts"):
        source, target = ROOT / name, tmp_path / name
        if source.is_dir():
            shutil.copytree(source, target, ignore=shutil.ignore_patterns("__pycache__"))
        else:
            shutil.copy(source, target)
    return tmp_path


def test_validator_accepts_explicit_bundled_server(tmp_path):
    copy_package(tmp_path)
    add_bundled_mcp(tmp_path)
    validator.validate_package(tmp_path)


@pytest.mark.parametrize("missing", ["mcp.json", ".mcp.json", "reference"])
def test_validator_rejects_partial_mcp_wiring(tmp_path, missing):
    copy_package(tmp_path)
    add_bundled_mcp(tmp_path)
    if missing == "reference":
        path = tmp_path / ".codex-plugin/plugin.json"
        compat = json.loads(path.read_text())
        del compat["mcpServers"]
        path.write_text(json.dumps(compat))
    else:
        (tmp_path / missing).unlink()
    with pytest.raises(ValueError, match="MCP"):
        validator.validate_package(tmp_path)


def test_validator_rejects_mcp_parity_mismatch(tmp_path):
    copy_package(tmp_path)
    add_bundled_mcp(tmp_path)
    (tmp_path / ".mcp.json").write_text('{"mcpServers": {}}')
    with pytest.raises(ValueError, match="MCP configurations differ"):
        validator.validate_package(tmp_path)


def test_source_archive_does_not_restore_global_mcp(tmp_path):
    from zipfile import ZipFile
    packager = load_script("package_local")
    archive = tmp_path / "plugin.zip"
    packager.package(ROOT, archive)
    with ZipFile(archive) as package:
        assert "mcp.json" not in package.namelist()
        assert ".mcp.json" not in package.namelist()
        assert "mcpServers" not in json.loads(package.read(".codex-plugin/plugin.json"))
        marketplace = json.loads(package.read(".agents/plugins/marketplace.json"))
        assert marketplace["plugins"][0]["source"] == {"source": "local", "path": "./"}


@pytest.mark.parametrize("source", [
    {"source": "local", "path": "../outside"},
    {"source": "local", "path": "/tmp/plugin"},
    {"source": "local", "path": "./missing"},
    {"source": "github", "repo": "example/not-published"},
])
def test_marketplace_rejects_non_repository_sources(tmp_path, source):
    root = copy_package(tmp_path)
    path = root / ".agents/plugins/marketplace.json"
    marketplace = json.loads(path.read_text())
    marketplace["plugins"][0]["source"] = source
    path.write_text(json.dumps(marketplace))
    with pytest.raises(ValueError, match="Marketplace source"):
        validator.validate_package(root)


def test_marketplace_identity_must_match_plugin(tmp_path):
    root = copy_package(tmp_path)
    path = root / ".agents/plugins/marketplace.json"
    marketplace = json.loads(path.read_text())
    marketplace["plugins"][0]["name"] = "unrelated-plugin"
    path.write_text(json.dumps(marketplace))
    with pytest.raises(ValueError, match="Marketplace and plugin identity"):
        validator.validate_package(root)


def test_archive_includes_only_marketplace_manifest_from_agents(tmp_path):
    from zipfile import ZipFile
    root = copy_package(tmp_path / "source")
    for relative in (".agents/private.json", ".venv/private.json", "runtime/private.json", "state/credentials.json"):
        path = root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("not packaged")
    archive = tmp_path / "plugin.zip"
    load_script("package_local").package(root, archive)
    with ZipFile(archive) as package:
        names = package.namelist()
        assert ".agents/plugins/marketplace.json" in names
        assert not any(name.startswith((".venv/", "runtime/", "state/")) for name in names)
        assert [name for name in names if name.startswith(".agents/")] == [".agents/plugins/marketplace.json"]


def set_onboarding(root, value, *, both=True):
    names = ("plugin.json", ".codex-plugin/plugin.json") if both else ("plugin.json",)
    for name in names:
        path = root / name
        data = json.loads(path.read_text())
        data.setdefault("extensions", {}).setdefault("com.openai", {})["onboardingSkill"] = value
        path.write_text(json.dumps(data))


def test_onboarding_skill_must_be_packaged(tmp_path):
    root = copy_package(tmp_path)
    (root / "skills" / "telegram-setup" / "SKILL.md").unlink()
    with pytest.raises(ValueError, match="Onboarding skill is missing"):
        validator.validate_package(root)


@pytest.mark.parametrize("value", ["/tmp/setup/SKILL.md", "./skills/../outside/SKILL.md", "./README.md", "skills/telegram-setup/SKILL.md"])
def test_onboarding_rejects_unscoped_paths(tmp_path, value):
    root = copy_package(tmp_path)
    set_onboarding(root, value)
    with pytest.raises(ValueError, match="Onboarding skill"):
        validator.validate_package(root)


def test_onboarding_rejects_symlink_escape(tmp_path):
    root = copy_package(tmp_path / "package")
    outside = tmp_path / "outside.md"
    outside.write_text("---\nname: external\ndescription: external\n---\n")
    target = root / "skills" / "telegram-setup" / "SKILL.md"
    target.unlink()
    target.symlink_to(outside)
    with pytest.raises(ValueError, match="Onboarding skill is missing or escapes"):
        validator.validate_package(root)


def test_onboarding_compatibility_must_match(tmp_path):
    root = copy_package(tmp_path)
    set_onboarding(root, "./skills/telegram-coordinate/SKILL.md", both=False)
    with pytest.raises(ValueError, match="onboarding skills differ"):
        validator.validate_package(root)
