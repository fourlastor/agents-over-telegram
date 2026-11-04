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
        if name not in {"get_me", "get_webhook_info", "get_updates", "send_message", "get_chat_member"}:
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
        # Tool approvals are optional here, so configs from older setups stay valid.
        _, current, proposed = _config_plan(project, _entry(core.state_dir, client, fake), approvals=False)
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
    if not (source / ".codex-plugin" / "plugin.json").is_file():
        raise CredentialError("plugin_template_missing_use_source_checkout")
    output.mkdir(parents=True, mode=0o700)
    for relative in (".codex-plugin", "skills", "docs/OPERATIONS.md", "docs/LIVE_VALIDATION.md", "docs/COMPATIBILITY.md", "docs/RELAY.md", "docs/DIRECT_DISPATCH.md", "docs/MACOS_SERVICE.md", "docs/BOT_EXCHANGES.md", "docs/HOSTS.md"):
        src, dst = source / relative, output / relative
        if src.is_dir():
            shutil.copytree(src, dst, ignore=shutil.ignore_patterns("__pycache__"))
        elif src.is_file():
            dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(src, dst)
    executable = str(Path(sys.executable).parent / "aot")
    entry = {"type": "stdio", "command": executable,
             "args": ["--state-dir", str(state_dir), "mcp", "--client-file", str(Path(client_file).absolute())]}
    (output / ".mcp.json").write_text(json.dumps({"mcpServers": {"telegram": entry}}, indent=2) + "\n")
    path = output / ".codex-plugin" / "plugin.json"
    manifest = json.loads(path.read_text())
    manifest["name"] = name
    # A rendered profile is already wired. Its setup action must not
    # create a second, project-native connection for the same client.
    manifest.get("extensions", {}).get("com.openai", {}).pop("onboardingSkill", None)
    manifest["mcpServers"] = "./.mcp.json"
    # Push hooks need a project's runtime pointer and the host
    # launcher; a rendered profile is a fixed client, so it ships neither.
    manifest.pop("hooks", None)
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
    setup.add_argument("--host", choices=["codex", "claude", "pi", "amp"], default="codex",
                       help="Agent host to prepare; codex writes .codex/config.toml, plugin hosts use the runtime pointer")
    register = sub.add_parser("account-register", help="Prompt privately for one bot token, validate without polling")
    register.add_argument("--receiver-handoff-confirmed", action="store_true", help="Assert other receivers are stopped; webhooks are never changed")
    sub.add_parser("account-status")
    create = sub.add_parser("client-create")
    create.add_argument("--label", required=True)
    create.add_argument("--client-file", type=Path, required=True)
    for name in ("pair-begin", "status", "history", "pause", "resume", "unsuspend", "disconnect", "mcp", "allow-sender", "bot-exchange-policy", "watch", "conversation-budget"):
        command = (sub.add_parser(name, help="Owner only: check with Telegram that a suspended destination works again, then clear it")
                   if name == "unsuspend" else sub.add_parser(name))
        command.add_argument("--client-file", type=Path, required=name not in {"status", "history", "pause", "resume", "unsuspend", "mcp", "watch"})
        if name == "unsuspend":
            command.add_argument("--topic-open", action="store_true",
                                 help="Confirm you checked that a closed or changed topic is open again")
        if name == "watch":
            command.add_argument("--collect", action="store_true", help="Also long-poll the shared Telegram collector; never sends or acknowledges")
            command.add_argument("--duration", type=float, default=0, help="Seconds to watch; 0 runs until stopped")
            command.add_argument("--interval", type=float, default=2.0, help="Seconds between local inbox checks")
            command.add_argument("--poll-timeout", type=int, default=10, help="Telegram long-poll seconds when collecting (0-20)")
            command.add_argument("--format", choices=["text", "jsonl"], default="text")
            command.add_argument("--no-backlog", action="store_true", help="Announce only deliveries that arrive after start")
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
        if name == "bot-exchange-policy":
            command.add_argument("--sender-id", type=int, required=True)
            change = command.add_mutually_exclusive_group()
            change.add_argument("--enable", dest="exchange_enabled", action="store_const", const=True)
            change.add_argument("--disable", dest="exchange_enabled", action="store_const", const=False)
        if name == "conversation-budget":
            command.add_argument("--conversation-id", required=True)
            command.add_argument("--additional-sends", type=int)
            command.add_argument("--duration-seconds", type=int)
            command.add_argument("--reason")
    confirm = sub.add_parser("pair-confirm")
    confirm.add_argument("--pairing-id", required=True)
    confirm.add_argument("--confirm", action="store_true", help="Confirm observed sender and destination after local inspection")
    limits = sub.add_parser("exchange-limits", help="Inspect or change the rolling send limits for every agent on this bot")
    limits.add_argument("--conversation-send-limit", type=int, help="Sends allowed per conversation in each rolling window")
    limits.add_argument("--conversation-window-seconds", type=int, help="Length of the per-conversation rolling window")
    limits.add_argument("--client-send-limit", type=int, help="Sends allowed per agent across all its conversations in each rolling window")
    limits.add_argument("--client-window-seconds", type=int, help="Length of the per-agent rolling window")
    limits.add_argument("--pair-send-limit", type=int, help="Sends allowed per external bot pair in each rolling window")
    limits.add_argument("--pair-window-seconds", type=int, help="Length of the per-pair rolling window")
    limits.add_argument("--reset", action="store_true", help="Return to the built-in defaults")
    limits.add_argument("--reason", help="Why the limits change; required when changing them")
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
    hook = sub.add_parser("hook", help="Answer one Claude Code or Codex hook event from stdin; never sends or acknowledges")
    hook.add_argument("--host", choices=["claude", "codex"], required=True)
    hook.add_argument("--project-dir", type=Path, help="Exact project directory; defaults to the host's project or cwd")
    hook.add_argument("--collect-interval", type=float, default=0,
                      help="Also collect Telegram updates when the last attempt is older than this many seconds (0 = never)")
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


def _confirm_operator(summary):
    """Ask the human at a terminal before raising or changing send limits.

    Agents usually run commands without a terminal, so this stops an agent from
    lifting its own limit by accident. It is not a security boundary: an agent
    running as the same user could still drive a terminal on purpose.
    """
    if not sys.stdin.isatty():
        raise CredentialError("operator_confirmation_required")
    print(summary + " Type yes to continue:", file=sys.stderr, flush=True)
    if input().strip().lower() != "yes":
        raise CredentialError("operator_confirmation_declined")


def run(args):
    from .core import Core, CoreError
    if args.command == "demo":
        from .demo import run_demo
        return run_demo()
    if args.command == "setup":
        from .onboarding import setup
        return setup(args)
    if args.command == "hook":
        from .hosts import run_hook
        return run_hook(args)
    from .onboarding import resolve_args
    args = resolve_args(args)
    state_dir = private_directory(args.state_dir)
    _mode(state_dir, args.fake)
    core = Core(state_dir, LazyTransport(state_dir, args.fake), recover_sends=(not args.command.startswith("relay-")
                and args.command not in {"history", "status", "account-status", "bot-exchange-policy", "watch",
                                         "conversation-budget", "exchange-limits"}))
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
    if args.command == "exchange-limits":
        changes = {"conversation_send_limit": args.conversation_send_limit,
                   "conversation_window_seconds": args.conversation_window_seconds,
                   "client_send_limit": args.client_send_limit,
                   "client_window_seconds": args.client_window_seconds,
                   "pair_send_limit": args.pair_send_limit, "pair_window_seconds": args.pair_window_seconds}
        if args.reset or any(value is not None for value in changes.values()):
            described = "reset to the defaults" if args.reset else ", ".join(
                f"{name}={value}" for name, value in changes.items() if value is not None)
            _confirm_operator(f"Change the Telegram send limits for every agent on this bot: {described}.")
        return core.exchange_limits(conversation_send_limit=args.conversation_send_limit,
                                    conversation_window_seconds=args.conversation_window_seconds,
                                    client_send_limit=args.client_send_limit,
                                    client_window_seconds=args.client_window_seconds,
                                    pair_send_limit=args.pair_send_limit,
                                    pair_window_seconds=args.pair_window_seconds,
                                    reset=args.reset, reason=args.reason)
    if args.command == "grant-peer":
        source = _client(core, args.source_client_file)
        target = _client(core, args.target_client_file)
        return core.grant_peer(source, target)
    if args.command == "resolve":
        return core.resolve_unknown(args.outbox_id, args.resolution, message_id=args.message_id)
    if args.client_file is None:
        raise CredentialError("client_file_required")
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
    if args.command == "bot-exchange-policy":
        return core.bot_exchange_policy(client_id, args.sender_id, enabled=args.exchange_enabled)
    if args.command == "unsuspend":
        return core.clear_suspension(client_id, topic_open_confirmed=args.topic_open)
    if args.command == "conversation-budget":
        if args.additional_sends is not None:
            _confirm_operator(f"Allow {args.additional_sends} more sends per rolling window in conversation "
                              f"{args.conversation_id!r} for {args.duration_seconds} seconds.")
        return core.conversation_budget(client_id, args.conversation_id,
                                        additional_sends=args.additional_sends,
                                        duration_seconds=args.duration_seconds, reason=args.reason)
    if args.command in {"pause", "resume"}:
        return core.pause(client_id, paused=args.command == "pause" and not args.resume)
    if args.command == "disconnect":
        return core.disconnect(client_id)
    if args.command == "plugin-render":
        if args.fake:
            raise CredentialError("fake_plugin_render_refused")
        return render_plugin(state_dir, args.client_file, args.output, args.name)
    if args.command == "watch":
        from .push import watch
        def emit(event):
            if args.format == "jsonl":
                print(json.dumps(event, separators=(",", ":")), flush=True)
            elif event.get("type") == "telegram_messages":
                print(event["notice"], flush=True)
            else:
                print("[Agents over Telegram] Watching stopped: " + event.get("reason", "unknown") + ".", flush=True)
        parent = os.getppid()
        # A host can die without stopping its watcher (for example on SIGKILL).
        # Re-parenting means the host is gone, so stop instead of polling forever.
        watch(core, client_id, emit, collect=args.collect, duration=args.duration, interval=args.interval,
              poll_timeout=args.poll_timeout, announce_backlog=not args.no_backlog,
              keep_running=lambda: os.getppid() == parent)
        return None
    if args.command == "mcp":
        from .mcp_server import serve
        serve(state_dir, args.client_file, transport=core.transport)
        return None
    raise CredentialError("unknown_command")


def _hook_invocation(argv):
    """True when argv selects the hook command, even if its options are invalid."""
    index = 0
    while index < len(argv):
        arg = argv[index]
        if arg == "--state-dir":
            index += 2
        elif arg == "--fake" or arg.startswith("--state-dir="):
            index += 1
        else:
            return arg == "hook"
    return False


def main():
    from .core import CoreError
    try:
        args = parser().parse_args()
    except SystemExit:
        # Exit status 2 blocks some host hook events (for example a user prompt).
        # A hook from a newer or older plugin must never do that.
        if _hook_invocation(sys.argv[1:]):
            return 0
        raise
    if args.command == "hook":
        # run_hook already turns failures into no output; this also covers a
        # host that closed the pipe before reading the answer.
        try:
            value = run(args)
            if value is not None:
                print(json.dumps(value, separators=(",", ":")), flush=True)
        except Exception:
            try:
                # Avoid a second failing flush at interpreter exit (status 120).
                os.dup2(os.open(os.devnull, os.O_WRONLY), sys.stdout.fileno())
            except (OSError, ValueError):
                pass
        return 0
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
        body = {"ok": False, "error": safe}
        details = getattr(error, "details", None)
        if isinstance(details, dict):
            # Rolling limit details are numbers only; drop anything else.
            numbers = {k: v for k, v in details.items() if isinstance(k, str) and re.fullmatch(r"[a-z_]{1,32}", k)
                       and isinstance(v, (int, float)) and not isinstance(v, bool)}
            if numbers:
                body["details"] = numbers
        print(json.dumps(body), file=sys.stderr)
        return 1
    except (KeyboardInterrupt, EOFError):
        print('{"ok":false,"error":"cancelled"}', file=sys.stderr)
        return 130
    except Exception:
        print('{"ok":false,"error":"operation_failed"}', file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
