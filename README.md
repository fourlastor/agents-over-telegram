# Agents over Telegram

Agents over Telegram (AOT) connects a coding agent project to a Telegram group or forum topic. You register **one bot once** and reuse it for many logical agents, each in its own project and conversation. Telegram shows the real shared bot, and each message carries a label that names the logical agent.

## Supported agent hosts

Version 0.11.4 supports **Claude Code**, **Codex**, **Pi**, **Amp** and **Antigravity** (the `agy` CLI and the desktop app), and, untested, **Grok Bot** (see [hosts](docs/HOSTS.md#grok-bot-untested)). Every host works the same way: you install AOT for the host once, then ask the agent to connect each project. A new Telegram message reaches the running agent without a typed prompt: an idle agent wakes up, and a busy agent gets a notice at its next safe point. The notice never contains the message text; the agent reads it with its scoped Telegram tools.

[Agent hosts](docs/HOSTS.md) explains how each integration works and lists exactly what was tested. Most host checks used real host processes with a fake Telegram transport, not live Telegram.

## Before you start

- **Linux or macOS.** Windows support is new and only tested in CI so far; see [Windows](docs/OPERATIONS.md#windows).
- **Python 3.11 or newer** with virtual-environment support. The agent prepares AOT's own runtime from it. uv is optional. On Debian or Ubuntu, install `python3-venv` if your Python does not include it. If Python is missing, see the official [Python downloads](https://www.python.org/downloads/).
- **A Telegram bot.** Create one with `@BotFather` if you do not have one for AOT yet. Add it to a dedicated group, or to a forum topic. Pairing works in a group or a forum topic only, not in a direct chat with the bot.

## Install AOT for your host (once)

Run the command for your host once. It installs the setup skill and the host's hooks, extension or plugin for your user, so every project can use them.

| Host | Install command |
|---|---|
| Claude Code | `claude plugin marketplace add fourlastor/agents-over-telegram && claude plugin install agents-over-telegram@agents-over-telegram --scope user` |
| Codex | `codex plugin marketplace add fourlastor/agents-over-telegram && codex plugin add agents-over-telegram@agents-over-telegram` |
| Pi | `pi install npm:agents-over-telegram` |
| Amp | `amp plugins add https://raw.githubusercontent.com/fourlastor/agents-over-telegram/main/integrations/amp/agents-over-telegram.ts && amp skill add fourlastor/agents-over-telegram/skills --global` |
| Antigravity | `git clone --depth 1 https://github.com/fourlastor/agents-over-telegram ~/.local/share/agents-over-telegram/source && agy plugin install ~/.local/share/agents-over-telegram/source` |

The Antigravity install works for both the `agy` CLI and the desktop app. In Codex, review and trust the plugin hooks once (the desktop app's hook review, or `/hooks` in the Codex CLI). Codex asks again after every plugin update.

## Connect a project

1. **Open the project and ask the agent "Connect this agent to Telegram".** The agent prepares AOT's runtime and runs setup for this project.
2. **The first time on a machine only:** if your bot is not registered here yet, the agent gives you one command to run in your own terminal. It asks for the bot token in a hidden prompt, so the token never passes through the chat.
3. **Post the pairing command.** The agent gives you a command such as `/pair@YourBot 1A2B3C4D5E6F`. Post it in the project's group or topic, then tell the agent you posted it. The agent finishes the connection and reports the group it connected to.
4. **Ask "Telegram status"** at any time to check the bot and the destination.

There is no second confirmation. The pairing code is single-use, expires after ten minutes and is shown only on your machine. When your bot already has connections, only their owner can complete a pairing by posting a code. A code that someone else posts stays pending, and you decide about it in your terminal. When the bot has no connection yet, the first post of the code connects, and the agent shows you where.

One project folder is one logical agent: every chat in that folder shares its Telegram identity. Use separate folders for separate agents. Setup writes `.aot-profile.json`, `.aot-runtime.json` and the host's MCP entry into the project. These files hold this machine's paths and the client id, not secrets. Setup adds the files it owns to the repository's local `.git/info/exclude`, and warns when git already tracks one of them.

In this README, `aot` means the runtime executable that setup names in the project's `.aot-runtime.json`. It is not a command on `PATH`. From the project folder, you can run it like this:

```sh
"$(python3 -c 'import json;print(json.load(open(".aot-runtime.json"))["executable"])')" tool messages_poll
```

For the terminal commands, offline bootstrap and existing-account options, see [local setup](docs/OPERATIONS.md#codex-setup). Host details, such as idle listening in the Codex desktop app, are in [agent hosts](docs/HOSTS.md).

## Updates

Update the host install the same way you installed it, then ask the agent to "update Agents over Telegram". The setup skill prepares the new runtime and moves the project to it with `setup --prepare-only`. The project keeps its client, its pairing and its allowed senders. In Claude Code the MCP server and hooks switch to the new runtime without a restart; see the [setup skill](skills/telegram-setup/SKILL.md#update-a-project-to-a-newer-version).

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

`message_send` and `owner_notify` show a small Markdown subset formatted in Telegram: inline code, code blocks, bold, italic, strikethrough, http(s) links, headings (shown bold) and bullets. AOT escapes everything else, so other Markdown, such as a table, shows as written. The agent label line always stays plain. If Telegram cannot parse the formatting, AOT sends the plain text instead. Approval requests are never formatted, so a link there always shows its target. In incoming messages, AOT writes code, code blocks and link targets back as Markdown, so the agent still sees them.

Incoming deliveries carry more than text:

- **Media.** A new connection accepts documents, photos, videos, audio, voice notes and animations. The agent downloads them with `attachment_fetch`. When the owner turns media off, a media message still arrives with a `media_refused` list instead of files.
- **Quoted replies and forwards.** A reply carries the message it replies to under `reply_to_message`, and the selected part under `reply_to_message.quote` when it quotes only part of it. A forwarded message carries its origin under `forwarded_from`.
- **Reactions.** People's reactions arrive as deliveries of kind `reaction`. Telegram sends reactions to a bot only while the bot is an administrator in the group. See [message reactions](docs/REACTIONS.md).

You can ask the agent in plain words, for example **"Telegram status"**, **"Pause Telegram"**, **"Resume Telegram"**, **"Show recent Telegram context"** or **"React 👍 to that message"**.

## Owner settings

The owner changes settings for one connection with `aot delivery-settings`. The command without options prints the current settings. The owner can also send **/settings** to the bot in a private chat to get a panel with every agent's settings as buttons (an ordinary button menu while Telegram refuses widgets from bots); see [settings panel](docs/OPERATIONS.md#settings-panel-in-telegram). For the panel and approvals to answer even while every agent is busy, run the [always-on collector](docs/OPERATIONS.md#always-on-collector) with `aot serve --install`. [Local operations](docs/OPERATIONS.md#addressed-only-delivery-and-group-context) explains every option.

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

AOT collects Telegram updates into a local inbox for each connection. In Claude Code, Pi, Amp and Antigravity, the host integration collects while the host is open. In Codex, the plugin hooks collect at most every 10 seconds during a turn, and the optional `relay-desktop` listener collects while it runs. Setup starts no background service.

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

A release is a version tag: pushing `vX.Y.Z` publishes the npm package and creates the GitHub release. See [releasing](docs/RELEASING.md).

Licensed under [Apache 2.0](LICENSE).
