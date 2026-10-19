#!/usr/bin/env python3
"""Offline fresh-project onboarding smoke; run with the installed runtime's Python.

Example: /absolute/runtime/bin/python scripts/smoke_onboarding.py \
    --aot /absolute/runtime/bin/aot --output /tmp/onboarding-smoke.json

The normal source package is extracted into a disposable simulated plugin cache.
All account data, project configurations and transport activity are fake and local.
This does not install the plugin in Codex or validate its installation UI.
"""
from __future__ import annotations

import argparse
import asyncio
from contextlib import AsyncExitStack
from datetime import datetime, timedelta, timezone
import json
import os
from pathlib import Path
import subprocess
import tempfile
import tomllib
import zipfile

from package_local import package


def require(condition, message):
    if not condition:
        raise AssertionError(message)


def fake_update(update_id, chat_id, text, label):
    return {"update_id": update_id, "message": {
        "message_id": update_id,
        "date": int(datetime.now(timezone.utc).timestamp()),
        "chat": {"id": chat_id, "type": "supergroup", "title": label + " smoke"},
        "from": {"id": 101, "is_bot": False}, "text": text,
    }}


async def check(aot):
    # These imports must come from the installed runtime, never a source override.
    from mcp import ClientSession, StdioServerParameters
    from mcp.client.stdio import stdio_client

    checks = []
    source = Path(__file__).resolve().parents[1]
    with tempfile.TemporaryDirectory(prefix="aot-fresh-onboarding-") as temporary:
        base = Path(temporary)
        state = base / "fake-state"
        cache = base / "plugin-cache" / "agents-over-telegram"
        archive_path = base / "normal-plugin.zip"
        count = package(source, archive_path)
        with zipfile.ZipFile(archive_path) as archive:
            names = set(archive.namelist())
            require(not names.intersection({"mcp.json", ".mcp.json"}),
                    "Normal package still contains an automatic MCP launcher")
            require(all(not Path(name).is_absolute() and ".." not in Path(name).parts
                        for name in names), "Package contains an unsafe extraction path")
            archive.extractall(cache)
        for manifest_name in ("plugin.json", ".codex-plugin/plugin.json"):
            manifest = json.loads((cache / manifest_name).read_text())
            require("mcpServers" not in manifest, "Manifest still declares automatic MCP")
            onboarding = manifest["extensions"]["com.openai"]["onboardingSkill"]
            skill = (cache / onboarding).resolve()
            require(skill.is_relative_to(cache) and skill.is_file(),
                    "Packaged onboarding skill is missing or outside the package")
        version = json.loads((cache / "plugin.json").read_text())["version"]
        checks.append("Normal source package extracts with valid onboarding skills and no automatic MCP launcher")

        env = {key: value for key, value in os.environ.items()
               if not key.startswith("AOT_") and key not in {"PYTHONPATH", "PYTHONHOME"}}
        # Even an unexpected fallback is confined to the disposable directory.
        env.update(XDG_DATA_HOME=str(base / "xdg-data"),
                   XDG_CONFIG_HOME=str(base / "xdg-config"),
                   XDG_CACHE_HOME=str(base / "xdg-cache"),
                   PYTHONNOUSERSITE="1")

        def cli(*args, cwd=cache, expected_error=None, text=False):
            result = subprocess.run([str(aot), *map(str, args)], cwd=cwd, env=env,
                                    capture_output=True, text=True, timeout=20)
            if expected_error is not None:
                require(result.returncode != 0, "Unconfigured project unexpectedly succeeded")
                require(json.loads(result.stderr).get("error") == expected_error,
                        "Unconfigured project did not fail with its expected scope error")
                return None
            require(result.returncode == 0, "CLI operation failed: " + result.stderr.strip())
            return result.stdout if text else json.loads(result.stdout)

        projects = {label: base / (label.lower() + "-project") for label in ("Alpha", "Beta")}
        prepared, config_bytes, profile_bytes = {}, {}, {}
        for label, project in projects.items():
            project.mkdir()
            require(not list(project.iterdir()), "Project was not freshly created")
            prepared[label] = cli("--state-dir", state, "--fake", "setup", "--label", label,
                                  "--project-dir", project, "--prepare-only")
            result = prepared[label]
            require(result["mode"] == "fake" and not result["host_trust_changed"]
                    and not result["automatic_delivery"], "Setup returned unexpected mode or authority")
            require((project / ".codex/config.toml").is_file(), "Setup did not create native MCP config")
            config_bytes[label] = (project / ".codex/config.toml").read_bytes()
            profile_bytes[label] = (project / ".aot-profile.json").read_bytes()
        require(prepared["Alpha"]["client_id"] != prepared["Beta"]["client_id"],
                "Fresh projects reused an identity")
        checks.append("Setup from the simulated plugin cache creates two fresh project profiles with distinct identities")

        fake_path = state / "fake-transport.json"

        def inject(updates):
            fake = json.loads(fake_path.read_text())
            fake["updates"].extend(updates)
            fake_path.write_text(json.dumps(fake))

        fake = json.loads(fake_path.read_text())
        require(not fake["sent"] and not fake["poll_offsets"], "Prepare-only performed transport work")
        require(not (state / "credentials.json").exists(), "Fake setup wrote bot credentials")
        checks.append("Prepare-only does not poll, send, or create bot credentials")
        destinations = {"Alpha": -1001, "Beta": -1002}
        inject([fake_update(index, destinations[label], prepared[label]["pairing_command"], label)
                for index, label in enumerate(projects, 1)])
        cli("--state-dir", state, "--fake", "collect")
        for label, project in projects.items():
            observed = cli("--state-dir", state, "--fake", "pair-confirm",
                           "--pairing-id", prepared[label]["pairing_id"])
            require(observed["sender_id"] == 101, "Pairing selected the wrong sender")
            cli("--state-dir", state, "--fake", "pair-confirm",
                "--pairing-id", prepared[label]["pairing_id"], "--confirm")
            status = cli("status", cwd=project)
            require(status["label"] == label and status["connected"]
                    and status["destination"]["chat_id"] == destinations[label],
                    "Pairing selected the wrong identity or destination")
            repeated = cli("--state-dir", state, "--fake", "setup", "--label", label,
                           "--project-dir", project, "--prepare-only")
            require(repeated["client_id"] == prepared[label]["client_id"], "Setup rerun changed identity")
            require((project / ".codex/config.toml").read_bytes() == config_bytes[label]
                    and (project / ".aot-profile.json").read_bytes() == profile_bytes[label],
                    "Setup rerun rewrote an existing profile or config")
        checks.append("Fake pairing binds intended destinations and setup reruns preserve config and identity byte for byte")

        # A nested project checks that profile lookup cannot inherit a parent's identity.
        third_project = projects["Alpha"] / "unconfigured-project"
        third_project.mkdir()
        before = fake_path.read_bytes()
        cli("status", cwd=third_project, expected_error="project_profile_missing_run_setup")
        require(not list(third_project.iterdir()) and fake_path.read_bytes() == before,
                "An unconfigured project inherited state or changed transport")
        checks.append("An unconfigured child project fails closed instead of inheriting Alpha's identity")

        async def tool(session, name, args=None):
            result = await session.call_tool(name, args or {})
            require(not result.isError and result.structuredContent
                    and result.structuredContent.get("ok") is True,
                    "MCP tool failed: " + name)
            return result.structuredContent["result"]

        async with asyncio.timeout(120), AsyncExitStack() as stack:
            sessions = {}
            for label, project in projects.items():
                entry = tomllib.loads(config_bytes[label].decode())["mcp_servers"]["telegram"]
                require(Path(entry["command"]).resolve() == aot.resolve(),
                        "Generated MCP config does not use the supplied installed runtime")
                require("--fake" in entry["args"], "Generated MCP config is not fake")
                receive, send = await stack.enter_async_context(stdio_client(StdioServerParameters(
                    command=entry["command"], args=entry["args"], env=env, cwd=str(project))))
                session = await stack.enter_async_context(ClientSession(
                    receive, send, read_timeout_seconds=timedelta(seconds=15)))
                initialized = await session.initialize()
                require(initialized.serverInfo.version == version,
                        "Installed MCP runtime version does not match the source package")
                sessions[label] = session
                status = await tool(session, "connection_status")
                require(status["client_id"] == prepared[label]["client_id"]
                        and status["destination"]["chat_id"] == destinations[label],
                        "MCP server has the wrong scoped identity")
                require(status["delivery"] == {"mode": "on_demand", "automatic": False}
                        and status["history"]["window_hours"] == 24,
                        "MCP status has unexpected delivery or history behavior")
            checks.append("Both newly generated native MCP commands launch with the installed package version and intended identity")
            before = fake_path.read_bytes()
            for session in sessions.values():
                await tool(session, "connection_status")
            require(fake_path.read_bytes() == before, "Status performed transport work")
            checks.append("MCP status reads perform no transport calls")

            cli("pause", cwd=projects["Alpha"])
            require((await tool(sessions["Alpha"], "connection_status"))["paused"], "Alpha did not pause")
            require(not (await tool(sessions["Beta"], "connection_status"))["paused"], "Alpha pause affected Beta")
            inject([fake_update(index, destinations[label], label + " fresh smoke", label)
                    for index, label in enumerate(projects, 3)])
            paused = await tool(sessions["Alpha"], "messages_poll")
            require(paused["paused"] and not paused["deliveries"], "Paused Alpha delivered work")
            beta = await tool(sessions["Beta"], "messages_poll")
            require([item["text"] for item in beta["deliveries"]] == ["Beta fresh smoke"],
                    "Beta did not receive exactly its own message")
            cli("resume", cwd=projects["Alpha"])
            alpha = await tool(sessions["Alpha"], "messages_poll")
            require([item["text"] for item in alpha["deliveries"]] == ["Alpha fresh smoke"],
                    "Resumed Alpha did not receive exactly its retained message")
            for label, batch in (("Alpha", alpha), ("Beta", beta)):
                await tool(sessions[label], "messages_ack", {
                    "delivery_ids": [item["delivery_id"] for item in batch["deliveries"]],
                    "lease_token": batch["lease_token"],
                })
                history = await tool(sessions[label], "messages_history", {"limit": 50})
                require(history["context_only"] and history["next_cursor"] is None,
                        "History returned unexpected pagination or context policy")
                require([item["text"] for item in history["messages"]] == [label + " fresh smoke"]
                        and all(item["status"] == "acknowledged" for item in history["messages"]),
                        "History lost acknowledged work or leaked another identity's messages")
                status = await tool(sessions[label], "connection_status")
                require(not status["paused"] and status["pending_messages"] == 0,
                        "Final connection state is not resumed and drained")
            checks.append("Alpha pause queues its message, Beta keeps working, and resume plus acknowledgment retains isolated history")

        human = cli("status", "--human", cwd=projects["Alpha"], text=True)
        require("Alpha" in human and "aot_test_bot" in human, "Human status did not show its scoped connection")
        require(not json.loads(fake_path.read_text())["sent"], "Smoke unexpectedly published a message")
        checks.append("Project-local human status works and the entire smoke publishes no messages")
        return {"status": "passed", "checked_at": datetime.now(timezone.utc).isoformat(),
                "version": version, "transport": "isolated offline fake", "source_files": count,
                "checks": checks, "limits": [
                    "No Telegram calls, real credentials, existing project configuration, or live state were used",
                    "Source package extraction and generated MCP commands were tested; Codex installation UI was not exercised",
                    "Automatic chat wakeup and Claude integration were not tested",
                ]}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--aot", required=True, type=Path, help="Absolute path to the installed aot executable")
    parser.add_argument("--output", type=Path, help="Optional JSON evidence file")
    args = parser.parse_args()
    if not args.aot.is_absolute() or not args.aot.is_file() or not os.access(args.aot, os.X_OK):
        parser.error("--aot must name an absolute, executable installed aot runtime")
    report = asyncio.run(check(args.aot))
    encoded = json.dumps(report, indent=2) + "\n"
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(encoded)
    print(encoded, end="")


if __name__ == "__main__":
    main()
