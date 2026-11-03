# Agents over Telegram

Connect a coding agent project to a Telegram group or forum topic. Register **one bot once**, then reuse it for multiple logical agents in separate project conversations. Telegram shows the actual shared bot; messages label the logical agent clearly.

## Supported agent hosts

Version 0.8.0 supports **Codex**, **Claude Code**, **Pi** and **Amp**. Each host uses its own plugin or extension system. A new Telegram message reaches the running agent without a typed prompt: an idle agent wakes up, and a busy agent gets a notice at its next safe point. The notice never contains the message text; the agent reads it with its scoped Telegram tools.

| Host | Integration | Push delivery |
|---|---|---|
| Codex | Codex plugin (skills and hooks) | `relay-desktop` wakes idle desktop chats; hooks reach busy turns |
| Claude Code | Claude Code plugin (MCP server, monitor, hooks) | A monitor wakes idle sessions; the monitor and hooks reach busy turns |
| Pi | Pi extension and `.pi/mcp.json` | The extension starts a turn or steers a busy agent |
| Amp | Amp plugin and `.amp/settings.json` | The plugin appends a steering message, opening a thread if needed |

Run setup with `--host codex|claude|pi|amp`. See [agent hosts](docs/HOSTS.md) for setup per host and for exactly what was tested. The new integrations were tested end to end with real host processes and a fake Telegram transport, not with live Telegram. The setup steps below describe Codex.

## First-time setup

You need Codex on Linux or macOS and **Python 3.11+ with virtual-environment support**. The plugin prepares its own stable runtime; **uv is optional**. If Python is missing, follow the official [Python downloads](https://www.python.org/downloads/). On Linux distributions that package virtual-environment support separately, install the matching package, such as `python3-venv` on Debian/Ubuntu. Then ask Codex to connect the project; you do not need a separate source checkout or virtual environment.

1. **Install the plugin from its GitHub marketplace:**

   ```sh
   codex plugin marketplace add fourlastor/agents-over-telegram
   codex plugin add agents-over-telegram@agents-over-telegram
   ```

   These are Codex CLI commands. Fresh installation from the public [fourlastor/agents-over-telegram repository](https://github.com/fourlastor/agents-over-telegram) has been verified.
2. **Prepare Telegram.** Create a bot with `@BotFather` if you do not already have one for this setup, and add it to a dedicated group or forum topic. Reuse that bot for later agents. Keep its token private; the wizard asks for it in a hidden terminal prompt. Direct-message pairing is not supported.
3. **Open the intended project in Codex and ask “Connect this agent to Telegram.”** The setup skill prepares the runtime from the installed plugin and gives you the private pairing command. Run the terminal wizard, send its exact `/pair@YourBotUsername …` command in the intended group/topic, then confirm the observed destination in the terminal. First registration also confirms that this bot has no other receiver. Adding another project reuses the registered bot without entering its token again.
4. **Load the connection.** Open/add the prepared project, make Codex's normal project-trust decision, then reload/reopen its chat. Ask **“Telegram status”** to verify the actual bot and destination through the loaded tools.
5. **Choose when to listen.** Ask **“Listen for Telegram in this chat for 10 minutes and reply to my messages.”** Keep ChatGPT and the listener running for that period. Open ChatGPT from the app list as usual. The listener supports Linux and macOS and checks the installed Codex queue capabilities. Live wakeup has been verified with bundled Codex **0.163.0-alpha.10.2**; other capable versions may run but do not yet have that live-test evidence.

**Every chat in a project shares its Telegram identity.** Use separate project directories for separate agents. Setup writes one native project `telegram` MCP entry and a profile reference; it does not configure a global plugin MCP server or change global trust. The runtime stays outside the plugin cache, so a cache refresh does not remove the configured executable. Existing configured projects keep their current runtime during ordinary setup.

For the terminal commands, offline bootstrap and existing-account options, see [local setup](docs/OPERATIONS.md#codex-setup).

## Everyday use

Ask Codex:

- **“Telegram status”** — show the actual bot, destination, pause state, backlog and next action.
- **“Pause Telegram” / “Resume Telegram”** — control this project's connection.
- **“Show recent Telegram context”** — read captured messages from the last 24 hours, including handled messages.
- **“Send Beta this testing result…”** — send within the recipient and purpose you authorize.
- **“Listen for Telegram”** — start a bounded listener for a chosen local chat while ChatGPT is open normally.
- **“Leave Telegram listening”** — keep the listener running until stopped. Automatic restart after login requires an explicit service request; see the [macOS service procedure](docs/MACOS_SERVICE.md).

AOT suspends a connection when the bot is removed or restricted, the topic is closed or edited, or the group moves to a new chat id. Queued messages are kept. Resume does not clear a suspension. After fixing the cause, the owner runs `aot unsuspend` on this computer; it checks with Telegram first. A moved group needs a new pairing instead. See [clear a suspended connection](docs/OPERATIONS.md#clear-a-suspended-connection).

Status and history do not contact Telegram or send messages. In Codex, collection is **on demand** by default: asking for new messages polls the shared collector and may progress previously authorized queued sends. Setup starts no listener or background service. The Claude Code, Pi and Amp integrations collect while the host is open, and the Codex plugin hooks collect at most every 10 seconds during a turn; see [agent hosts](docs/HOSTS.md).

The optional [desktop listener](docs/DIRECT_DISPATCH.md) uses native Codex queueing and a wake URL to notify an existing local chat. **Open ChatGPT from the app list as usual.** No custom app launcher, protocol wrapper or relay model is required. Listening follows the requested duration or runs continuously when explicitly requested; the target uses its own scoped Telegram tools to handle pending work. Without a duration or ongoing-listening request, the default check lasts 55 seconds.

On Linux with bundled Codex **0.163.0-alpha.10.2**, the live `relay-desktop` check passed with ChatGPT opened normally: Telegram marker `normal-idle-1` woke the existing local Alpha chat, which polled once, sent one reply and acknowledged the delivery after publication was recorded as sent. No model forwarded the wake after the marker arrived, and Alpha returned to zero pending messages. Native tool and ledger evidence confirm the round trip; the user also confirmed the reply is visible in Telegram. The [compatibility record](docs/COMPATIBILITY.md) separates this result from earlier relay and shared-backend experiments.

History covers captured local records, not a complete prior-day Telegram transcript. Messages consumed elsewhere or missed before binding and the old bridge's archive are not imported.

On macOS, the listener discovers the bundled native CLI inside ChatGPT.app or Codex.app, validates the running app and its direct local backend with libproc/sysctl, and opens the wake URL in that same app. Follow-up live checks passed a full Telegram → idle chat → reply/ack round trip. Responding during an active work turn was verified with a separate, operator-installed hook that is not shipped in the plugin; the native queue alone can wait until that turn ends. See [macOS follow-up validation](docs/COMPATIBILITY.md#macos-follow-up-idle-and-busy-tests).

## Validation and setup details

Version 0.8.0 adds push delivery for four hosts. Its full suite passed **764 passed, 2 skipped**, and its host checks are listed in [agent hosts](docs/HOSTS.md#what-was-tested). The 0.7.2 results below still describe the Codex installation path.

Version 0.7.2 passed a fresh install from the public GitHub repository with **no uv executable available**, followed by automatic pip bootstrap, repeat reuse, cache-independent runtime operation and fresh fake Alpha/Beta scoped MCP checks. The release suite passed **572 tests in 21.71 seconds**. Earlier live Telegram checks covered peer delivery, pause/resume and normal-desktop wake/reply/ack. The desktop plugin UI, automatic onboarding button and a fresh real-bot registration remain untested. See [implementation status](docs/IMPLEMENTATION_STATUS.md) for the evidence and limits.

- [Agent hosts: Claude Code, Codex, Pi and Amp](docs/HOSTS.md)
- [Guided setup, local controls and advanced CLI](docs/OPERATIONS.md)
- [Listen with the normally opened desktop](docs/DIRECT_DISPATCH.md)
- [Shared binding procedure and older model-mediated relay](docs/RELAY.md)
- [Compatibility and scope boundaries](docs/COMPATIBILITY.md)
- [Live validation and deliberate receiver migration](docs/LIVE_VALIDATION.md)
- [Implementation and validation evidence](docs/IMPLEMENTATION_STATUS.md)
- [Original proposal](PROPOSAL.md) and [handoff](HANDOFF.md)

## Offline development

Python 3.11+ is required; Linux with Python 3.13.7 was tested. The development workflow below uses uv, although ordinary plugin setup does not require it. From the source root:

```sh
uv sync --frozen --extra dev --python /usr/bin/python3.13
.venv/bin/aot demo
.venv/bin/python -m pytest -q
.venv/bin/python scripts/validate_package.py
```

The demo and automated tests use isolated fake state. They need no token and do not call Telegram. The lockfile pins dependencies, including the official Python MCP SDK. Source/runtime installation does not install a daemon or select a live account.

`scripts/e2e_fake.py` prepares an isolated fake-Telegram project for any host and injects test messages, so a real Claude Code, Codex, Pi or Amp process can be checked end to end without a token. See [agent hosts](docs/HOSTS.md#what-was-tested).

Licensed under [Apache 2.0](LICENSE).
