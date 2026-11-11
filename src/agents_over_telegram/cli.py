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

from . import compat
from .credentials import CredentialError, private_directory, read_private_json, validate_token, write_private_json
from .transport import FakeTransport, TelegramTransport, TransportError, make_transport


# Every Telegram call Core makes. A method missing here fails as AttributeError,
# which Core records as an unknown send without ever reaching Telegram.
LAZY_METHODS = frozenset({"get_me", "get_webhook_info", "get_updates", "send_message", "send_photo",
                          "send_document", "forward_message", "set_message_reaction", "get_chat_member",
                          "download_file", "answer_callback_query", "edit_message_text", "send_widget",
                          "answer_widget_query", "edit_message_widget_data", "edit_ephemeral_message_text",
                          "set_my_commands", "delete_message"})


class LazyTransport:
    def __init__(self, state_dir, fake):
        self.state_dir, self.fake = state_dir, fake
        self.transport = None

    def __getattr__(self, name):
        if name not in LAZY_METHODS:
            raise AttributeError(name)
        if self.transport is None:
            self.transport = make_transport(self.state_dir, fake=self.fake)
        return getattr(self.transport, name)


MAX_TOOL_ARGUMENTS = 1024 * 1024


def _tool_arguments():
    """The tool arguments as one JSON object on stdin; nothing on stdin means none."""
    if sys.stdin is None or sys.stdin.isatty():
        return {}
    raw = sys.stdin.buffer.read(MAX_TOOL_ARGUMENTS + 1)
    if len(raw) > MAX_TOOL_ARGUMENTS:
        return []  # not an object, so the tool refuses it as invalid_arguments
    if not raw.strip():
        return {}
    try:
        return json.loads(raw)
    except (ValueError, UnicodeError):
        return []


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
    executable = str(compat.own_aot())
    entry = {"type": "stdio", "command": executable,
             "args": ["--state-dir", str(state_dir), "mcp", "--client-file", str(Path(client_file).absolute())]}
    (output / ".mcp.json").write_text(json.dumps({"mcpServers": {"telegram": entry}}, indent=2) + "\n", encoding='utf-8')
    path = output / ".codex-plugin" / "plugin.json"
    manifest = json.loads(path.read_text(encoding='utf-8'))
    manifest["name"] = name
    # A rendered profile is already wired. Its setup action must not
    # create a second, project-native connection for the same client.
    manifest.get("extensions", {}).get("com.openai", {}).pop("onboardingSkill", None)
    manifest["mcpServers"] = "./.mcp.json"
    # Push hooks need a project's runtime pointer and the host
    # launcher; a rendered profile is a fixed client, so it ships neither.
    manifest.pop("hooks", None)
    path.write_text(json.dumps(manifest, indent=2) + "\n", encoding='utf-8')
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
    serve_choice = setup.add_mutually_exclusive_group()
    serve_choice.add_argument("--serve", action="store_true",
                              help="Install the always-on collector (aot serve) as a login service; ask the human first")
    serve_choice.add_argument("--no-serve", action="store_true",
                              help="Do not install the always-on collector, and do not ask")
    setup.add_argument("--host", choices=["codex", "claude", "pi", "amp"], default="codex",
                       help="Agent host to prepare; codex writes .codex/config.toml, plugin hosts use the runtime pointer")
    register = sub.add_parser("account-register", help="Prompt privately for one bot token, validate without polling")
    register.add_argument("--receiver-handoff-confirmed", action="store_true", help="Assert other receivers are stopped; webhooks are never changed")
    sub.add_parser("account-status")
    sub.add_parser("codex-hooks", help="Show whether Codex still runs AOT's plugin hooks or wants them "
                                        "approved again with /hooks (read-only)")
    judge_config = sub.add_parser("judge-config", help="Owner only: configure, show or remove the decision model "
                                                       "for the optional model-judged wake decision "
                                                       "(delivery-settings --model-judge)")
    judge_config.add_argument("action", choices=["set", "status", "clear"])
    judge_config.add_argument("--endpoint", help="System One decision endpoint: Jev, Cloudflare Clef or a "
                                                 "self-hosted Clef; https, or http to this computer only")
    judge_config.add_argument("--model", help="Model name the endpoint expects, for example jev-latest or clef-flash")
    judge_config.add_argument("--threshold", type=float, default=0.5,
                              help="Deliver when the model's probability for yes is at least this (0.5 to 0.99)")
    judge_config.add_argument("--key", action="store_true",
                              help="Ask for an API key in a hidden prompt; leave it out for a local model")
    tool = sub.add_parser("tool", help="Run one Telegram tool for this project's agent, as the MCP server does; "
                                       "JSON arguments on stdin")
    tool.add_argument("name", help="Tool name, for example messages_poll, message_send or messages_ack")
    tool.add_argument("--client-file", type=Path)
    create = sub.add_parser("client-create")
    create.add_argument("--label", required=True)
    create.add_argument("--client-file", type=Path, required=True)
    for name in ("pair-begin", "status", "history", "pause", "resume", "unsuspend", "disconnect", "mcp", "allow-sender", "bot-exchange-policy", "delivery-settings", "watch", "conversation-budget", "settings-panel"):
        command = (sub.add_parser(name, help="Owner only: check with Telegram that a suspended destination works again, then clear it")
                   if name == "unsuspend" else
                   sub.add_parser(name, help="Send the owner a settings panel for this connection in the private chat with the bot")
                   if name == "settings-panel" else sub.add_parser(name))
        command.add_argument("--client-file", type=Path, required=name not in {"status", "history", "pause", "resume", "unsuspend", "mcp", "watch"})
        if name == "unsuspend":
            command.add_argument("--topic-open", action="store_true",
                                 help="Confirm you checked that a closed or changed topic is open again")
        if name == "watch":
            command.add_argument("--host", choices=["claude"],
                                 help="Name the host, so notices add its shell fallback")
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
            kind = command.add_mutually_exclusive_group()
            kind.add_argument("--bot", action="store_true")
            kind.add_argument("--remove", action="store_true",
                              help="stop accepting this sender; auto-allow will not add it back until it is "
                                   "allowed again explicitly")
        if name == "delivery-settings":
            command.add_argument("--filter", dest="delivery_filter", choices=["all", "addressed"],
                                 help="all (default): deliver every allowed message; addressed: deliver only "
                                      "messages that mention the bot, reply to it or address a command to it, "
                                      "and keep the rest as context")
            command.add_argument("--reaction-wake", dest="reaction_wake",
                                 choices=["own_messages", "all", "none"],
                                 help="own_messages (default): deliver and wake only reactions on messages this "
                                      "agent published; all: every allowed reaction; none: keep all reactions "
                                      "as context without waking")
            command.add_argument("--context", dest="capture_context", choices=["on", "off"],
                                 help="on: also keep messages from senders that are not allowed, as context only")
            command.add_argument("--media", dest="accept_media", choices=["on", "off"],
                                 help="on: accept documents, photos, video, audio, voice notes and animations, "
                                      "with their caption as text")
            command.add_argument("--wake-interval", dest="wake_interval", type=int, metavar="SECONDS",
                                 help="batch notices: after one, wait this long before the next, which then "
                                      "covers everything that arrived meanwhile; 0 (default) announces at once")
            command.add_argument("--wake-exempt", dest="wake_exempt",
                                 choices=["addressed,owner", "addressed", "owner", "none"],
                                 help="what is still announced at once while batching: addressed,owner "
                                      "(default) messages that address the bot and messages from the owner")
            command.add_argument("--owner-notify", dest="owner_notify", choices=["on", "off"],
                                 help="on: let this agent send private notices to the owner with owner_notify "
                                      "(at most 3 per hour and 10 per day; off by default)")
            command.add_argument("--model-judge", dest="model_judge", choices=["on", "off"],
                                 help="on: with --filter addressed, ask a decision model whether a person's message "
                                      "kept as context is meant for this agent, and deliver it if so; sends recent "
                                      "group messages to the model; needs judge-config set (off by default)")
            command.add_argument("--auto-allow", dest="auto_allow", choices=["on", "off"],
                                 help="on: add every person and bot who posts in this group or topic to the allowed "
                                      "senders, for groups where everyone is trusted (off by default; off keeps the "
                                      "senders added so far)")
            command.add_argument("--keep-alive", dest="keep_alive", choices=["on", "off"],
                                 help="on: Claude Code only; when an idle session's background listener reaches its "
                                      "23-hour limit, wake the session for one short turn so a new listener starts "
                                      "(off by default: after 23 idle hours the session stops hearing Telegram)")
            command.add_argument("--approvals-in", dest="approvals_in", choices=["private", "group"],
                                 help="where owner approvals are asked: private (default) in the owner's private "
                                      "chat with the bot, or group in this group or topic as a message only the "
                                      "owner sees; Telegram does not guarantee those, so a failed send falls back "
                                      "to the private chat")
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
    serve = sub.add_parser("serve", help="Collect from Telegram without stopping, so owner actions and messages "
                                         "are handled even while no agent is free")
    action = serve.add_mutually_exclusive_group()
    action.add_argument("--install", action="store_true", help="Run it as a user service (systemd or launchd)")
    action.add_argument("--uninstall", action="store_true", help="Stop and remove the user service")
    action.add_argument("--status", action="store_true", help="Show whether the user service is set up")
    action.add_argument("--on-demand", action="store_true",
                        help="Started by an agent: stop when idle, when the service is installed or a newer runtime takes over")
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
    wake = sub.add_parser("wake", help="Claude Code asyncRewake hook: wait for new deliveries, then exit 2 to wake the session")
    wake.add_argument("--host", choices=["claude"], required=True)
    wake.add_argument("--project-dir", type=Path, help="Exact project directory; defaults to the host's project or cwd")
    wake.add_argument("--session", default="", help="Host session id; one waiter runs per client and session")
    wake.add_argument("--host-pid", type=int, help="Host process; the waiter stops when it is gone")
    wake.add_argument("--duration", type=float, default=82800, help="Seconds to wait before exiting quietly")
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


def judge_config(state_dir, args):
    """Configure, show or remove the decision model; the key is never printed."""
    from . import judge
    if args.action != "set":
        if args.endpoint or args.model or args.key or args.threshold != judge.DEFAULT_THRESHOLD:
            raise CredentialError("judge_options_only_with_set")
        return judge.config_status(state_dir) if args.action == "status" else {"cleared": judge.clear_config(state_dir)}
    if not args.endpoint or not args.model:
        raise CredentialError("judge_endpoint_and_model_required")
    api_key = ""
    if args.key:
        if not sys.stdin.isatty():
            raise CredentialError("private_terminal_required")
        api_key = getpass.getpass("Decision model API key (hidden, stored locally): ")
        if not api_key.strip():
            raise CredentialError("invalid_judge_key")
    judge.store_config(state_dir, args.endpoint, args.model, api_key, args.threshold)
    return judge.config_status(state_dir)


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


def _confirm_or_ask_owner(core, client_id, summary, action, params):
    """Confirm at the terminal, or without one ask the owner with Telegram buttons.

    Returns None when the terminal confirmed, so the caller applies the change
    now; otherwise the pending approval, which the owner's button press applies.
    """
    if sys.stdin.isatty():
        _confirm_operator(summary)
        return None
    return core.request_owner_approval(client_id, action, params, summary)


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
    if args.command == "wake":
        from .hosts import run_wake
        return run_wake(args)
    from .onboarding import resolve_args
    args = resolve_args(args)
    if args.command == "codex-hooks":
        from . import codex_trust
        return codex_trust.status()
    state_dir = private_directory(args.state_dir)
    _mode(state_dir, args.fake)
    core = Core(state_dir, LazyTransport(state_dir, args.fake), recover_sends=(not args.command.startswith("relay-")
                and args.command not in {"history", "status", "account-status", "judge-config", "bot-exchange-policy", "delivery-settings", "watch",
                                         "conversation-budget", "exchange-limits", "serve"}))
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
    if args.command == "judge-config":
        return judge_config(state_dir, args)
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
    if args.command == "serve":
        from . import serve
        if args.install:
            return serve.install(state_dir)
        if args.uninstall:
            # Removing it on purpose also means no collector started on demand;
            # serve --install or setup --serve turns collecting back on.
            result = serve.uninstall(state_dir)
            serve.opt_out(state_dir)
            return {**result, 'on_demand': False}
        if args.status:
            return serve.status(state_dir)
        from .relay_api import Relay
        raise SystemExit(serve.run(core, state_dir, watchdog=serve.Watchdog(), relay=Relay(core, state_dir),
                                   on_demand=args.on_demand))
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
    if args.command == "tool":
        from . import relay_api
        from .mcp_server import _TOOL_MAP, _VALIDATORS, call_tool, relayed
        arguments = _tool_arguments()
        if args.name in _TOOL_MAP and isinstance(arguments, dict) and _VALIDATORS[args.name].is_valid(arguments):
            secret = read_private_json(args.client_file)["client_secret"]
            body = relayed(lambda name, values: relay_api.call(state_dir, secret, name, values), args.name, arguments,
                           args.client_file)
            if body is not None:
                return body
        return call_tool(core, client_id, args.name, arguments)
    if args.command == "pair-begin":
        return core.connection_begin(client_id)
    if args.command == "status":
        return core.connection_status(client_id)
    if args.command == "history":
        return core.history(client_id, limit=args.limit, cursor=args.cursor)
    if args.command == "configure-peer":
        return core.configure_peer_command(client_id, args.bot_username, args.peer_command)
    if args.command == "allow-sender":
        if args.remove:
            return core.remove_sender(client_id, args.sender_id)
        return core.allow_sender(client_id, args.sender_id, is_bot=args.bot)
    if args.command == "bot-exchange-policy":
        return core.bot_exchange_policy(client_id, args.sender_id, enabled=args.exchange_enabled)
    if args.command == "delivery-settings":
        def switch(value):
            return None if value is None else value == "on"
        changes = {"delivery_filter": args.delivery_filter, "capture_context": switch(args.capture_context),
                   "accept_media": switch(args.accept_media), "reaction_wake": args.reaction_wake,
                   "wake_interval": args.wake_interval, "wake_exempt": args.wake_exempt,
                   "owner_notify": switch(args.owner_notify), "model_judge": switch(args.model_judge),
                   "auto_allow": switch(args.auto_allow), "keep_alive": switch(args.keep_alive),
                   "approvals_in": args.approvals_in}
        warnings = []
        if args.model_judge == "on":
            warnings.append("With the addressed filter, recent messages from this group or topic will be sent "
                            "to the configured decision model (aot judge-config status) to decide whether a "
                            "person's message is meant for this agent.")
        if args.auto_allow == "on":
            warnings.append("Every person and bot who posts in this group or topic will become an allowed sender "
                            "and can give this agent work. That includes anyone who joins later, for example "
                            "through an invite link. Remove one with allow-sender --remove.")
        if args.keep_alive == "on":
            warnings.append("Claude Code only: when this idle session's background listener reaches its 23-hour "
                            "limit, it wakes the session for one short turn so a new listener starts. Each such "
                            "turn reads the whole conversation without the prompt cache, once per idle day.")
        if warnings:
            # The whole command waits for the owner, so nothing applies half.
            pending = _confirm_or_ask_owner(core, client_id, " ".join(warnings), "delivery_settings",
                                            {name: value for name, value in changes.items() if value is not None})
            if pending is not None:
                return pending
        return core.delivery_settings(client_id, **changes)
    if args.command == "settings-panel":
        return core.send_settings_panel(client_id)
    if args.command == "unsuspend":
        return core.clear_suspension(client_id, topic_open_confirmed=args.topic_open)
    if args.command == "conversation-budget":
        if args.additional_sends is not None:
            pending = _confirm_or_ask_owner(
                core, client_id,
                f"Allow {args.additional_sends} more sends per rolling window in conversation "
                f"{args.conversation_id!r} for {args.duration_seconds} seconds.",
                "conversation_budget",
                {"conversation_id": args.conversation_id, "additional_sends": args.additional_sends,
                 "duration_seconds": args.duration_seconds, "reason": args.reason})
            if pending is not None:
                return pending
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
                from .push import with_shell_fallback
                print(with_shell_fallback(event["notice"]) if args.host == "claude" else event["notice"], flush=True)
            else:
                print("[Agents over Telegram] Watching stopped: " + event.get("reason", "unknown") + ".", flush=True)
        from .hosts import RUNTIME_MOVED_EXIT, runtime_moved
        parent = os.getppid()
        project = Path.cwd().resolve()
        moved = []

        def keep_running():
            # A host can die without stopping its watcher (for example on SIGKILL).
            # Re-parenting means the host is gone, so stop instead of polling forever.
            if os.getppid() != parent:
                return False
            # A launcher that follows the runtime restarts this watcher on a new one.
            if runtime_moved(project):
                moved.append(True)
                return False
            return True

        from .relay_api import Waiter
        waiter = Waiter(state_dir, read_private_json(args.client_file)["client_secret"])
        watch(core, client_id, emit, collect=args.collect, duration=args.duration, interval=args.interval,
              poll_timeout=args.poll_timeout, announce_backlog=not args.no_backlog,
              keep_running=keep_running, wait_for_change=waiter)
        if moved:
            raise SystemExit(RUNTIME_MOVED_EXIT)
        return None
    if args.command == "mcp":
        from .hosts import _start_collector
        from .mcp_server import serve
        _start_collector(state_dir)
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
            return arg in ("hook", "wake")
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
    if args.command == "wake":
        # Only a real wake may exit 2, and only a moved runtime 3 (for the
        # launcher to restart it); anything else must stay silent.
        from .hosts import RUNTIME_MOVED_EXIT
        try:
            value = run(args)
            return value if value in (2, RUNTIME_MOVED_EXIT) else 0
        except BaseException:
            return 0
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
            elif args.command == "tool":
                print(json.dumps(value, ensure_ascii=False, separators=(",", ":")), flush=True)
                return 0 if value.get("ok") else 1
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
