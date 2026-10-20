import json
from pathlib import Path
import subprocess
import sys

from agents_over_telegram.cli import parser, run
from agents_over_telegram.core import Core
from agents_over_telegram.credentials import read_private_json, write_private_json
from agents_over_telegram.demo import run_demo
from agents_over_telegram.transport import FakeTransport


def command(state, *args):
    return run(parser().parse_args(["--state-dir", str(state), "--fake", *args]))


def test_private_cli_setup_once_two_clients_and_pairing(tmp_path):
    state = tmp_path / "state"
    first = command(state, "account-register")
    assert command(state, "account-register") == first
    assert not (state / "credentials.json").exists()
    client_a = state / "clients" / "alpha.json"
    client_b = state / "clients" / "beta.json"
    a = command(state, "client-create", "--label", "Alpha", "--client-file", str(client_a))
    b = command(state, "client-create", "--label", "Beta", "--client-file", str(client_b))
    assert "client_secret" not in a
    pairs = [command(state, "pair-begin", "--client-file", str(path)) for path in (client_a,client_b)]
    fake = read_private_json(state / "fake-transport.json")
    fake["updates"] = [{"update_id": i, "message": {"message_id": i, "chat": {"id": -100, "type": "supergroup"},
        "message_thread_id": i, "from": {"id": 101, "is_bot": False}, "text": pair["command"]}} for i,pair in enumerate(pairs,1)]
    write_private_json(state / "fake-transport.json", fake, overwrite=True)
    assert command(state, "collect")["updates"] == 2
    for pair in pairs:
        preview = command(state, "pair-confirm", "--pairing-id", pair["pairing_id"])
        assert preview
        result = command(state, "pair-confirm", "--pairing-id", pair["pairing_id"], "--confirm")
        assert result["connected"]
    command(state, "grant-peer", "--source-client-file", str(client_a), "--target-client-file", str(client_b))
    command(state, "allow-sender", "--client-file", str(client_a), "--sender-id", "202", "--bot")
    policy_args = ("bot-exchange-policy", "--client-file", str(client_a), "--sender-id", "202")
    assert not command(state, *policy_args)["enabled"]
    assert command(state, *policy_args, "--enable")["enabled"]
    assert command(state, *policy_args)["pair_send_limit"] == 32
    assert not command(state, *policy_args, "--disable")["enabled"]
    command(state, "configure-peer", "--client-file", str(client_a), "--bot-username", "peer_bot", "--command", "review")
    assert len(command(state, "account-status")["accounts"]) == 1
    assert command(state, "pause", "--client-file", str(client_a))["paused"]
    assert not command(state, "status", "--client-file", str(client_b))["paused"]
    assert command(state, "disconnect", "--client-file", str(client_a))["revoked"]


def test_mode_mismatch_does_not_touch_token(tmp_path):
    state = tmp_path / "state"
    command(state, "account-register")
    process = subprocess.run([sys.executable,"-m","agents_over_telegram","--state-dir",str(state),"collect"],capture_output=True,text=True)
    assert process.returncode == 1
    assert "state_transport_mode_mismatch" in process.stderr


def test_live_setup_requires_explicit_receiver_handoff_before_prompt(tmp_path):
    process = subprocess.run([sys.executable,"-m","agents_over_telegram","--state-dir",str(tmp_path/"state"),"account-register"],capture_output=True,text=True)
    assert process.returncode == 1
    assert "receiver_handoff_confirmation_required" in process.stderr
    assert "token" not in process.stderr


def test_plugin_renderer_has_no_secret_or_token_and_valid_schema(tmp_path):
    from jsonschema import Draft202012Validator
    from importlib.util import module_from_spec, spec_from_file_location
    root = Path(__file__).resolve().parents[1]
    state = tmp_path / "state"
    core = Core(state, FakeTransport())
    core.register_account(900001, "aot_test_bot")
    client = core.create_client("Alpha")
    client_path = state / "clients" / "alpha.json"
    write_private_json(client_path, client)
    rendered = tmp_path / "rendered"
    result = run(parser().parse_args(["--state-dir",str(state),"plugin-render","--client-file",str(client_path),"--output",str(rendered),"--name","agents-over-telegram-alpha"]))
    assert not result["installed"]
    for path in rendered.rglob("*"):
        if path.is_file():
            assert client["client_secret"] not in path.read_text()
    for name in ("plugin", "mcp"):
        schema = json.loads((root/"docs"/"schemas"/(name+".schema.json")).read_text())
        Draft202012Validator(schema).validate(json.loads((rendered/(name+".json")).read_text()))
    manifest = json.loads((rendered/"mcp.json").read_text())
    server = next(iter(manifest["mcpServers"].values()))
    assert server["args"][-1] == str(client_path)
    assert "AOT_CLIENT_FILE" not in json.dumps(manifest)
    assert json.loads((rendered/".mcp.json").read_text()) == {"mcpServers": manifest["mcpServers"]}
    compat = json.loads((rendered/".codex-plugin/plugin.json").read_text())
    assert compat["mcpServers"] == "./.mcp.json"
    for name in ("plugin.json", ".codex-plugin/plugin.json"):
        metadata = json.loads((rendered/name).read_text())
        assert "onboardingSkill" not in metadata.get("extensions", {}).get("com.openai", {})
    spec = spec_from_file_location("package_validator", root / "scripts/validate_package.py")
    validator = module_from_spec(spec)
    spec.loader.exec_module(validator)
    validator.validate_package(rendered)


def test_demo_is_offline_and_temporary():
    result = run_demo()
    assert result["result"] == "PASS"
    assert result["agents"] == 2 and result["bot_accounts"] == 1
