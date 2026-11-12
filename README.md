# Agents over Telegram

Agents over Telegram (AOT) connects a coding agent project to a Telegram group or forum topic. You register **one bot once** and reuse it for many logical agents, each in its own project and conversation. Telegram shows the real shared bot, and each message carries a label that names the logical agent.

## Supported agent hosts

Version 0.9.37 supports **Codex**, **Claude Code**, **Pi** and **Amp**, and, untested, **Grok Bot** (see [hosts](docs/HOSTS.md#grok-bot-untested)). Each host uses its own plugin or extension system. A new Telegram message reaches the running agent without a typed prompt: an idle agent wakes up, and a busy agent gets a notice at its next safe point. The notice never contains the message text; the agent reads it with its scoped Telegram tools.

| Host | Integration | Push delivery |
|---|---|---|
| Claude Code | Claude Code plugin (MCP server, hooks, monitor) | A background `asyncRewake` hook wakes idle sessions, also in the desktop app. Hooks reach busy turns. |
| Codex | Codex plugin (skills and hooks) | The optional `relay-desktop` listener wakes idle desktop chats. Hooks reach busy turns. |
| Pi | Pi extension and `.pi/mcp.json` | The extension starts a turn or steers a busy agent. |
| Amp | Amp plugin and `.amp/settings.json` | The plugin appends a steering message and opens a thread if needed. |

Setup takes `--host claude|codex|pi|amp`. [Agent hosts](docs/HOSTS.md) has the setup steps for each host and lists exactly what was tested. Most host checks used real host processes with a fake Telegram transport, not live Telegram. That page says which checks used live Telegram.

## Before you start

- **Linux or macOS.** Windows support is new and only tested in CI so far; see [Windows](docs/OPERATIONS.md#windows).
- **Python 3.11 or newer** with virtual-environment support. The plugin prepares its own runtime from it. uv is optional. On Debian or Ubuntu, install `python3-venv` if your Python does not include it. If Python is missing, see the official [Python downloads](https://www.python.org/downloads/).
- **A Telegram bot.** Create one with `@BotFather` if you do not have one for AOT yet. Add it to a dedicated group, or to a forum topic. Keep the token private: the setup wizard asks for it in a hidden terminal prompt. Later agents reuse the same bot without the token.

Pairing works in a group or a forum topic only. AOT does not pair a direct-message chat with the bot.

## Connect a project in Claude Code

1. **Install the plugin for your user:**

   ```sh
   claude plugin marketplace add fourlastor/agents-over-telegram
   claude plugin install agents-over-telegram@agents-over-telegram --scope user
   ```

   User scope is the recommended default. Every later session then starts with the plugin loaded, so you can connect a project without a restart. To keep the plugin out of other projects, install it with `--scope local` inside the project folder instead.
2. **Open the project and ask "Connect this agent to Telegram".** The setup skill prepares the runtime and gives you the pairing command. Run the terminal wizard yourself, send its exact `/pair@YourBotUsername …` command in the chosen group or topic, then confirm the observed destination in the terminal.
3. **Ask "Telegram status"** to check the bot and the destination.

You do not need to restart Claude Code. The Telegram tools appear in the open session when setup finishes. If the plugin was installed or enabled during the session, type `/reload-plugins` once: `/reload-plugins --force` in the terminal CLI, plain `/reload-plugins` in the desktop app. Keep the session open. The plugin collects Telegram updates for the project, so you need no separate listener.

In the desktop app, Claude Code connects a newly added plugin MCP server only in the next session. Until then the agent uses the same tools from a shell with `aot tool <name>`, with JSON arguments on stdin. These commands apply the same checks as the MCP server. See [Claude Code](docs/HOSTS.md#claude-code) for details.

In this README, `aot` means the runtime executable that setup names in the project's `.aot-runtime.json`. It is not a command on `PATH`. From the project folder, you can run it like this:

```sh
"$(python3 -c 'import json;print(json.load(open(".aot-runtime.json"))["executable"])')" tool messages_poll
```

The hook and monitor notices in Claude Code also print the full path of this executable.

## Connect a project in Codex

1. **Install the plugin from its GitHub marketplace:**

   ```sh
   codex plugin marketplace add fourlastor/agents-over-telegram
   codex plugin add agents-over-telegram@agents-over-telegram
   ```

2. **Open the project in Codex and ask "Connect this agent to Telegram".** Pair in the terminal as described for Claude Code above.
3. **Load the connection.** Open the prepared project, make Codex's normal project-trust decision, then reload or reopen its chat. Review and trust the plugin hooks once with `/hooks` in the Codex CLI. Ask **"Telegram status"** to check the connection.
4. **Optional: listen while the chat is idle.** Ask **"Listen for Telegram in this chat for 10 minutes"**. Open ChatGPT from the app list as usual. The [desktop listener](docs/DIRECT_DISPATCH.md) wakes the chat when a message arrives.

Setup writes one `telegram` MCP entry in the project's `.codex/config.toml`. It does not configure a global MCP server or change global trust. Setup also approves the project's `messages_poll`, `message_send`, `message_react` and `messages_ack` tools, so Codex's approval reviewer does not block work that a hook notice started. Every chat in a project shares its Telegram identity, so use separate project folders for separate agents.

For the terminal commands, offline bootstrap and existing-account options, see [local setup](docs/OPERATIONS.md#codex-setup). For Pi and Amp, see [agent hosts](docs/HOSTS.md#pi).

## Updates

Claude Code updates a connected project in place, without a restart:

1. Update the plugin with `claude plugin update agents-over-telegram@agents-over-telegram`.
2. Prepare the new runtime from the updated plugin, as in first setup.
3. Run `setup --prepare-only` with the **new** runtime's executable. This step moves the project to the new runtime: it rewrites `.aot-runtime.json` and keeps the client, its pairing and its allowed senders.

After that, the plugin's MCP server switches to the new runtime before the next tool call, and Claude Code fetches the tools again. Hooks and the wake waiter use the new runtime from their next event. Only changed hooks, skills or monitors in the plugin need `/reload-plugins`. The [setup skill](skills/telegram-setup/SKILL.md#update-a-claude-code-project-to-a-newer-version) has the exact commands.

## What an agent can do

The agent uses these MCP tools. Each tool works only on the agent's own connection.

| Tool | What it does |
|---|---|
| `messages_poll`, `messages_ack` | Read new deliveries, then confirm them after handling. |
| `message_send` | Send a labeled message. It can reply to a message (`reply_to`) and attach one image (`image_path`: PNG, JPEG, WebP or GIF, up to 10 MB). |
| `message_react` | Set or remove the bot's emoji reaction on a message in the agent's own group or topic. |
| `messages_history` | Read captured messages from the last 24 hours. |
| `attachment_fetch` | Download a file from an incoming message into private local state. |
| `owner_notify` | Send a short private notice to the owner. It is off until the owner turns it on. |
| `delivery_status` | Check the outcome of a send, reaction or owner notice without sending again. |
| `connection_status`, `connections_list`, `accounts_status` | Show the bot, the destination, the pause state and the next step. |
| `connection_begin`, `connection_pause`, `connection_disconnect` | Start a pairing, pause or resume, or revoke this agent's connection. |

Incoming deliveries carry more than text:

- **Media.** A new connection accepts documents, photos, videos, audio, voice notes and animations. The agent downloads them with `attachment_fetch`. When the owner turns media off, a media message still arrives with a `media_refused` list instead of files.
- **Quoted replies and forwards.** A reply carries the message it replies to under `reply_to_message`. A forwarded message carries its origin under `forwarded_from`.
- **Reactions.** People's reactions arrive as deliveries of kind `reaction`. Telegram sends reactions to a bot only while the bot is an administrator in the group. See [message reactions](docs/REACTIONS.md).

You can ask the agent in plain words, for example **"Telegram status"**, **"Pause Telegram"**, **"Resume Telegram"**, **"Show recent Telegram context"** or **"React 👍 to that message"**.

## Owner settings

The owner changes settings for one connection with `aot delivery-settings`. The command without options prints the current settings. The owner can also send **/settings** to the bot in a private chat to get a panel with every agent's settings as buttons; see [settings panel](docs/OPERATIONS.md#settings-panel-in-telegram). For the panel and approvals to answer even while every agent is busy, run the [always-on collector](docs/OPERATIONS.md#always-on-collector) with `aot serve --install`. [Local operations](docs/OPERATIONS.md#addressed-only-delivery-and-group-context) explains every option.

| Option | Default | Effect |
|---|---|---|
| `--filter all\|addressed` | `all` | With `addressed`, AOT delivers only messages that mention the bot, reply to it or address a command to it, plus a person's quick follow-ups to such a message. It keeps the other messages as context. |
| `--context on\|off` | `off` | Keeps messages from senders who are not allowed as context. AOT never delivers them. |
| `--media on\|off` | `on` for new connections | Accepts media files. A connection paired before 0.9.4 keeps its earlier setting, which was off. |
| `--reaction-wake own_messages\|all\|none` | `own_messages` | Chooses which reactions AOT delivers. |
| `--wake-interval SECONDS` | `0` | Batches wake-up notices. After one notice, AOT waits this long and then announces everything that arrived in one notice. |
| `--wake-exempt addressed,owner\|addressed\|owner\|none` | `addressed,owner` | Chooses which messages AOT still announces at once during the wake interval. |
| `--model-judge on\|off` | `off` | With `--filter addressed`, asks a decision model (Jev, Cloudflare Clef or a self-hosted Clef) whether a person's message kept as context is meant for this agent, and delivers it if so. Needs `aot judge-config set`, and sends recent group messages to that model. See [model-judged wake decision](docs/OPERATIONS.md#model-judged-wake-decision). |
| `--auto-allow on\|off` | `off` | Adds every person and bot who posts in the group or topic to the allowed senders, for groups where everyone is trusted. Asks the owner to confirm at a terminal, or with Telegram buttons when no terminal is attached. Remove a sender with `allow-sender --remove`. See [auto-allow](docs/OPERATIONS.md#auto-allow-in-a-trusted-group). |
| `--owner-notify on\|off` | `off` | Allows `owner_notify` for this connection. |

**Owner notices.** With `owner_notify`, an agent can reach the owner in a private chat with the bot, for a decision, a blocker or an approval. AOT allows at most 3 notices per agent in any rolling hour and 10 in any rolling day. The owner must send `/start` to the bot once in a private chat, because Telegram does not let a bot start a private chat. The bot answers to confirm it is ready. See [notify the owner privately](docs/OPERATIONS.md#notify-the-owner-privately).

**Suspended connections.** AOT suspends a connection when the bot is removed or loses the right to post, or when the bound topic is closed or edited. AOT keeps queued messages. After fixing the cause, the owner runs `aot unsuspend` on this computer. It checks with Telegram first. See [clear a suspended connection](docs/OPERATIONS.md#clear-a-suspended-connection).

**Group upgrades.** When Telegram turns a group into a supergroup, the group gets a new chat id. AOT moves the connection to the new chat id by itself, with its allowed senders, held messages and queued sends. The owner does nothing. A connection that an older AOT version suspended for this reason moves when the owner runs `aot unsuspend`. See [group upgrades](docs/OPERATIONS.md#group-upgrades-to-a-supergroup).

## How messages arrive

AOT collects Telegram updates into a local inbox for each connection. In Claude Code, Pi and Amp, the host integration collects while the host is open. In Codex, the plugin hooks collect at most every 10 seconds during a turn, and the optional `relay-desktop` listener collects while it runs. Setup starts no background service.

The `relay-desktop` listener uses native Codex queueing and a wake URL to wake an existing local chat. It does not queue a wake while the chat is busy, because the Codex hooks then deliver the notice inside that turn. When a queue command ends without a receipt, the listener settles the result itself from the desktop's own records. See [listen with the desktop](docs/DIRECT_DISPATCH.md).

In a supergroup without forum topics, a reply reaches the group's connection. Telegram marks such a reply with a thread id, but AOT reads that id as a topic only in a forum.

Status and history read local records only. They do not contact Telegram or send messages. History covers messages that AOT captured locally. It is not a full transcript of the group.

## Documentation

- [Agent hosts: Claude Code, Codex, Pi and Amp](docs/HOSTS.md)
- [Guided setup, owner settings, local controls and advanced CLI](docs/OPERATIONS.md)
- [Message reactions](docs/REACTIONS.md)
- [Listen with the normally opened desktop](docs/DIRECT_DISPATCH.md)
- [Bot-to-bot exchanges](docs/BOT_EXCHANGES.md)
- [Shared binding procedure and older model-mediated relay](docs/RELAY.md)
- [Compatibility and evidence boundaries](docs/COMPATIBILITY.md)
- [macOS service](docs/MACOS_SERVICE.md)
- [Live validation and deliberate receiver migration](docs/LIVE_VALIDATION.md)
- [Implementation and validation evidence](docs/IMPLEMENTATION_STATUS.md)
- [Original proposal](PROPOSAL.md) and [handoff](HANDOFF.md)

## Development

The development workflow uses uv; ordinary plugin setup does not need it. From the source root:

```sh
uv sync --frozen --extra dev
.venv/bin/aot demo
.venv/bin/python -m pytest -q
.venv/bin/python scripts/validate_package.py
```

The demo and the tests use isolated fake state. They need no token and do not call Telegram. `scripts/e2e_fake.py` prepares an isolated fake-Telegram project for any host and injects test messages, so you can check a real Claude Code, Codex, Pi or Amp process end to end without a token. See [what was tested](docs/HOSTS.md#what-was-tested).

Licensed under [Apache 2.0](LICENSE).
