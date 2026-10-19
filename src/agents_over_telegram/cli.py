"""Private operator setup and explicitly scoped MCP launches."""
from __future__ import annotations

import argparse
import getpass
import hashlib
import json
import os
from pathlib import Path
import shutil
import sys
import tempfile

from .credentials import CredentialError, private_directory, read_private_json, validate_token, write_private_json
from .transport import FakeTransport, TelegramTransport, TransportError, make_transport


class LazyTransport:
    def __init__(self, state_dir, fake):
        self.state_dir, self.fake = state_dir, fake
        self.transport = None

    def __getattr__(self, name):
        if name not in {"get_me", "get_webhook_info", "get_updates", "send_message"}:
            raise AttributeError(name)
        if self.transport is None:
            self.transport = make_transport(self.state_dir, fake=self.fake)
        return getattr(self.transport, name)


def _emit(value):
    print(json.dumps(value, indent=2, sort_keys=True))


def _client(core, path, expected=None, *, allow_revoked=False):
    value = read_private_json(path)
    secret = value.get("client_secret", "")
    if allow_revoked:
        # Operator-only relay metadata/cleanup remains possible after disconnect.
        # This never reactivates credentials or permits Telegram/MCP operations.
        if not isinstance(secret, str) or len(secret) > 256:
            raise CredentialError('authentication_failed')
        with core._db() as db:
            row = db.execute('SELECT id FROM clients WHERE secret_hash=?',
                             (hashlib.sha256(secret.encode()).hexdigest(),)).fetchone()
        if row is None:
            raise CredentialError('authentication_failed')
        client_id = row['id']
    else:
        client_id = core.authenticate(secret)
    if value.get("client_id") != client_id:
        raise CredentialError("client_identity_mismatch")
    if expected is not None and client_id != expected:
        raise CredentialError("project_profile_client_mismatch")
    return client_id


def _mode(state_dir, fake):
    path = state_dir / "mode.json"
    if path.exists() and read_private_json(path).get("mode") != ("fake" if fake else "live"):
        raise CredentialError("state_transport_mode_mismatch")


def _relay_project_valid(core, binding, fake):
    """Fail closed if a previously bound project's profile or MCP entry drifts."""
    from .core import CoreError
    from .onboarding import read_profile, _config_plan, _entry
    try:
        project = Path(binding['project_dir'])
        profile = read_profile(project)
        if (profile is None or profile['client_id'] != binding['client_id']
                or Path(profile['state_dir']).resolve() != core.state_dir.resolve()
                or profile['mode'] != ('fake' if fake else 'live')):
            return False
        client = Path(profile['client_file'])
        _client(core, client, binding['client_id'])
        _, current, proposed = _config_plan(project, _entry(core.state_dir, client, fake))
        return current is not None and current == proposed
    except (CredentialError, CoreError, OSError, ValueError):
        return False


def render_plugin(state_dir, client_file, output, name):
    import re
    if not re.fullmatch(r"[a-z0-9]+(?:-[a-z0-9]+)*", name) or len(name) > 64:
        raise CredentialError("invalid_plugin_name")
    output = Path(output).absolute()
    if output.exists():
        raise CredentialError("plugin_output_exists")
    source = Path(__file__).resolve().parents[2]
    if not (source / "plugin.json").is_file():
        raise CredentialError("plugin_template_missing_use_source_checkout")
    output.mkdir(parents=True, mode=0o700)
    for relative in ("plugin.json", ".codex-plugin", "skills", "docs/OPERATIONS.md", "docs/LIVE_VALIDATION.md", "docs/COMPATIBILITY.md", "docs/RELAY.md", "docs/DIRECT_DISPATCH.md"):
        src, dst = source / relative, output / relative
        if src.is_dir():
            shutil.copytree(src, dst, ignore=shutil.ignore_patterns("__pycache__"))
        elif src.is_file():
            dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(src, dst)
    executable = str(Path(sys.executable).parent / "aot")
    entry = {"type": "stdio", "command": executable,
             "args": ["--state-dir", str(state_dir), "mcp", "--client-file", str(Path(client_file).absolute())]}
    for filename in ("mcp.json", ".mcp.json"):
        manifest = {"mcpServers": {"telegram": entry}}
        if filename == "mcp.json":
            manifest["$schema"] = "https://agent-plugins.org/schemas/1.0.0/mcp.schema.json"
        (output / filename).write_text(json.dumps(manifest, indent=2) + "\n")
    for filename in ("plugin.json", ".codex-plugin/plugin.json"):
        path = output / filename
        if path.exists():
            manifest = json.loads(path.read_text())
            manifest["name"] = name
            # A rendered profile is already wired. Its setup action must not
            # create a second, project-native connection for the same client.
            manifest.get("extensions", {}).get("com.openai", {}).pop("onboardingSkill", None)
            if filename == ".codex-plugin/plugin.json":
                manifest["mcpServers"] = "./.mcp.json"
            path.write_text(json.dumps(manifest, indent=2) + "\n")
    return {"plugin_directory": str(output), "name": name, "installed": False,
            "scope": "One client identity per server process; separate chat isolation requires host support."}


def parser():
    p = argparse.ArgumentParser(description="One shared human-owned Telegram bot; private local setup.")
    p.add_argument("--state-dir", type=Path, default=None)
    p.add_argument("--fake", action="store_true", default=None, help="Isolated offline transport; never reads a bot token")
    sub = p.add_subparsers(dest="command", required=True)
    setup = sub.add_parser("setup", help="Prepare and pair one agent for an exact local project directory")
    setup.add_argument("--label", required=True)
    setup.add_argument("--project-dir", type=Path, required=True)
    setup.add_argument("--client-file", type=Path, help="Reuse this existing protected client file")
    setup.add_argument("--receiver-handoff-confirmed", action="store_true")
    setup.add_argument("--prepare-only", action="store_true", help="Prepare profile and pairing without collecting or confirming")
    register = sub.add_parser("account-register", help="Prompt privately for one bot token, validate without polling")
    register.add_argument("--receiver-handoff-confirmed", action="store_true", help="Assert other receivers are stopped; webhooks are never changed")
    sub.add_parser("account-status")
    create = sub.add_parser("client-create")
    create.add_argument("--label", required=True)
    create.add_argument("--client-file", type=Path, required=True)
    for name in ("pair-begin", "status", "history", "pause", "resume", "disconnect", "mcp", "allow-sender"):
        command = sub.add_parser(name)
        command.add_argument("--client-file", type=Path, required=name not in {"status", "history", "pause", "resume"})
        if name == "status":
            command.add_argument("--human", action="store_true", help="Show a concise local status instead of JSON")
        if name == "history":
            command.add_argument("--limit", type=int, default=20)
            command.add_argument("--cursor")
        if name == "pause":
            command.add_argument("--resume", action="store_true")
        if name == "allow-sender":
            command.add_argument("--sender-id", type=int, required=True)
            command.add_argument("--bot", action="store_true")
    confirm = sub.add_parser("pair-confirm")
    confirm.add_argument("--pairing-id", required=True)
    confirm.add_argument("--confirm", action="store_true", help="Confirm observed sender and destination after local inspection")
    sub.add_parser("collect")
    sub.add_parser("unassigned")
    flush = sub.add_parser("flush")
    flush.add_argument("--client-file", type=Path)
    peer = sub.add_parser("configure-peer")
    peer.add_argument("--client-file", type=Path, required=True)
    peer.add_argument("--bot-username", required=True)
    peer.add_argument("--command", dest="peer_command", required=True)
    grant = sub.add_parser("grant-peer")
    grant.add_argument("--source-client-file", type=Path, required=True)
    grant.add_argument("--target-client-file", type=Path, required=True)
    resolve = sub.add_parser("resolve")
    resolve.add_argument("--outbox-id", required=True)
    resolve.add_argument("--resolution", choices=["accepted", "cancel", "retry"], required=True)
    resolve.add_argument("--message-id", type=int)
    render = sub.add_parser("plugin-render")
    render.add_argument("--client-file", type=Path, required=True)
    render.add_argument("--output", type=Path, required=True)
    render.add_argument("--name", default="agents-over-telegram")
    relay_bind = sub.add_parser("relay-bind", help="Bind one client to an explicitly authorized local Codex chat")
    relay_bind.add_argument("--client-file", type=Path, required=True)
    relay_bind.add_argument("--thread-id", required=True)
    relay_bind.add_argument("--project-dir", type=Path, required=True)
    relay_bind.add_argument("--include-pending", action="store_true", help="Notify existing unhandled work instead of baselining it")
    relay_unbind = sub.add_parser("relay-unbind")
    relay_unbind.add_argument("--client-file", type=Path, required=True)
    relay_status = sub.add_parser("relay-status")
    relay_status.add_argument("--client-file", type=Path)
    relay_resolve = sub.add_parser("relay-resolve", help="Record host handoff outcome; never acknowledges inbox work")
    relay_resolve.add_argument("--notification-id", required=True)
    relay_resolve.add_argument("--outcome", choices=["accepted", "failed", "uncertain", "cancelled"], required=True)
    relay_watch = sub.add_parser("relay-watch", help="Bounded session relay; host messaging is performed by the waiting chat")
    relay_watch.add_argument("--once", action="store_true", help="Exit promptly after one event, for bounded Codex shell waits")
    relay_watch.add_argument("--duration", type=float, default=55)
    relay_watch.add_argument("--interval", type=float, default=1.5)
    relay_watch.add_argument("--collect", action="store_true", help="Also run the shared Telegram collector; never flush outgoing sends")
    dispatch = sub.add_parser("relay-dispatch", help="Wake mapped Codex chats through their explicitly shared app-server")
    dispatch.add_argument("--endpoint", required=True, help="Private unix:///path socket or authenticated loopback WebSocket")
    dispatch.add_argument("--token-file", type=Path, help="Protected JSON bearer-token file for loopback WebSocket")
    dispatch.add_argument("--duration", type=float, default=55, help="Listening seconds; 0 continues until explicitly stopped")
    dispatch.add_argument("--collect", action="store_true", help="Also collect Telegram updates; never flush or acknowledge work")
    desktop = sub.add_parser("relay-desktop", help="Wake mapped local chats in an already running, normally launched desktop")
    desktop.add_argument("--codex-binary", type=Path,
                         help="Bundled native Codex CLI (auto-detected on Linux/macOS)")
    desktop.add_argument("--codex-home", type=Path, help="Existing Codex home used by the running desktop")
    desktop.add_argument("--duration", type=float, default=55, help="Listening seconds; 0 continues until explicitly stopped")
    desktop.add_argument("--collect", action="store_true", help="Also collect Telegram updates; never flush or acknowledge work")
    sub.add_parser("demo", help="Run a standalone temporary two-agent offline demonstration")
    return p


def register_account(core, state_dir, fake, receiver_handoff_confirmed):
    if not fake and not receiver_handoff_confirmed:
        raise CredentialError("receiver_handoff_confirmation_required")
    credential_path = state_dir / "credentials.json"
    if fake:
        transport = make_transport(state_dir, fake=True)
    else:
        if not credential_path.exists() and not sys.stdin.isatty():
            raise CredentialError("private_terminal_required")
        token = validate_token(read_private_json(credential_path)["token"] if credential_path.exists()
                               else getpass.getpass("Telegram bot token (hidden, stored locally): "))
        transport = TelegramTransport(token)
    bot = transport.get_me()
    if transport.get_webhook_info().get("url"):
        raise CredentialError("existing_webhook_refused")
    result = core.register_account(bot["id"], bot["username"])
    if not fake and not credential_path.exists():
        write_private_json(credential_path, {"token": token})
    if not (state_dir / "mode.json").exists():
        write_private_json(state_dir / "mode.json", {"mode": "fake" if fake else "live"})
    return {"registered": True, "bot_id": bot["id"], "username": bot["username"], "mode": "fake" if fake else "live"}


def run(args):
    from .core import Core, CoreError
    if args.command == "demo":
        from .demo import run_demo
        return run_demo()
    if args.command == "setup":
        from .onboarding import setup
        return setup(args)
    from .onboarding import resolve_args
    args = resolve_args(args)
    state_dir = private_directory(args.state_dir)
    _mode(state_dir, args.fake)
    core = Core(state_dir, LazyTransport(state_dir, args.fake), recover_sends=(not args.command.startswith("relay-")
                and args.command not in {"history", "status", "account-status"}))
    if args.command.startswith("relay-"):
        from .relay import RelayStore
        store = RelayStore(core, project_validator=lambda binding: _relay_project_valid(core, binding, args.fake))
        if args.command == "relay-desktop":
            from .desktop_dispatcher import run_desktop_dispatcher
            run_desktop_dispatcher(core, store, codex_binary=args.codex_binary, codex_home=args.codex_home,
                                   duration=args.duration, collect=args.collect)
            return None
        if args.command == "relay-dispatch":
            from .dispatcher import run_dispatcher
            result = run_dispatcher(core, store, args.endpoint, token_file=args.token_file,
                                    duration=args.duration, collect=args.collect)
            if result['reason'] == 'connection_failed':
                raise CoreError('dispatcher_connection_failed')
            return None
        if args.command == "relay-watch":
            from .relay_watch import watch
            watch(core, store, once=args.once, duration=args.duration,
                  interval=args.interval, collect=args.collect)
            return None
        if args.command == "relay-resolve":
            return store.resolve(args.notification_id, args.outcome)
        client_id = (_client(core, args.client_file, args.expected_client_id,
                             allow_revoked=args.command in {'relay-status', 'relay-unbind'})
                     if args.client_file is not None else None)
        if args.command == "relay-status":
            return store.status(client_id)
        if args.command == "relay-unbind":
            return store.unbind(client_id)
        if args.command == "relay-bind":
            from .onboarding import read_profile
            profile = read_profile(args.project_dir.resolve())
            if (profile is None or profile['client_id'] != client_id
                    or Path(profile['state_dir']).resolve() != state_dir.resolve()
                    or profile['mode'] != ('fake' if args.fake else 'live')
                    or Path(profile['client_file']).resolve() != args.client_file.resolve()):
                raise CredentialError('relay_project_profile_mismatch')
            if not _relay_project_valid(core, {'client_id': client_id,
                                               'project_dir': str(args.project_dir.resolve())}, args.fake):
                raise CredentialError('relay_project_profile_mismatch')
            return store.bind(client_id, args.thread_id, args.project_dir,
                              include_pending=args.include_pending)
    if args.command == "account-register":
        return register_account(core, state_dir, args.fake, args.receiver_handoff_confirmed)
    if args.command == "client-create":
        if args.client_file.exists():
            raise CredentialError("client_file_exists")
        private_directory(args.client_file.absolute().parent)
        result = core.create_client(args.label)
        write_private_json(args.client_file, result)
        return {"client_id": result["client_id"], "client_file": str(args.client_file.absolute()), "label": args.label}
    if args.command == "pair-confirm":
        return core.confirm_pairing(args.pairing_id) if args.confirm else core.pairing_status(args.pairing_id)
    if args.command == "collect":
        return core.collect()
    if args.command == "unassigned":
        return core.unassigned()
    if args.command == "flush":
        return core.flush(client_id=_client(core, args.client_file, args.expected_client_id) if args.client_file else None)
    if args.command == "account-status":
        return core.accounts_status()
    if args.command == "grant-peer":
        source = _client(core, args.source_client_file)
        target = _client(core, args.target_client_file)
        return core.grant_peer(source, target)
    if args.command == "resolve":
        return core.resolve_unknown(args.outbox_id, args.resolution, message_id=args.message_id)
    client_id = _client(core, args.client_file, args.expected_client_id)
    if args.command == "pair-begin":
        return core.connection_begin(client_id)
    if args.command == "status":
        return core.connection_status(client_id)
    if args.command == "history":
        return core.history(client_id, limit=args.limit, cursor=args.cursor)
    if args.command == "configure-peer":
        return core.configure_peer_command(client_id, args.bot_username, args.peer_command)
    if args.command == "allow-sender":
        return core.allow_sender(client_id, args.sender_id, is_bot=args.bot)
    if args.command in {"pause", "resume"}:
        return core.pause(client_id, paused=args.command == "pause" and not args.resume)
    if args.command == "disconnect":
        return core.disconnect(client_id)
    if args.command == "plugin-render":
        if args.fake:
            raise CredentialError("fake_plugin_render_refused")
        return render_plugin(state_dir, args.client_file, args.output, args.name)
    if args.command == "mcp":
        from .mcp_server import serve
        serve(state_dir, args.client_file, transport=core.transport)
        return None
    raise CredentialError("unknown_command")


def main():
    from .core import CoreError
    args = parser().parse_args()
    try:
        value = run(args)
        if value is not None:
            if args.command == "setup" and not args.prepare_only:
                from .onboarding import human_status
                print(human_status(value["connection"]))
                print("Next: " + json.dumps(value["next_step"], ensure_ascii=True)[1:-1])
            elif args.command == "status" and args.human:
                from .onboarding import human_status
                print(human_status(value))
            else:
                _emit(value)
        return 0
    except (CredentialError, CoreError, TransportError) as error:
        # Even custom/fake exceptions cannot smuggle URLs or response data out.
        import re
        code = str(error.code)
        safe = code if re.fullmatch(r"[a-z0-9_]{1,64}", code) else "operation_failed"
        print(json.dumps({"ok": False, "error": safe}), file=sys.stderr)
        return 1
    except (KeyboardInterrupt, EOFError):
        print('{"ok":false,"error":"cancelled"}', file=sys.stderr)
        return 130
    except Exception:
        print('{"ok":false,"error":"operation_failed"}', file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
