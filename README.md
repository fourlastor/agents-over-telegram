# Agents over Telegram

Connect a Codex project to a Telegram group or forum topic. Register **one bot once**, then reuse it for multiple logical agents in separate project conversations. Telegram shows the actual shared bot; messages label the logical agent clearly.

## First-time setup

You need Codex on Linux, **Python 3.11+** and **uv**. If a prerequisite is missing, follow the official [Python downloads](https://www.python.org/downloads/) or [uv installation guide](https://docs.astral.sh/uv/getting-started/installation/). The plugin prepares its own stable runtime; you do not need a separate source checkout or virtual environment.

1. **Install the plugin from its GitHub marketplace.** No GitHub repository has been published for this package yet. Once it is published, replace the `OWNER/REPO` placeholder below with that repository; do not run the placeholder literally:

   ```sh
   codex plugin marketplace add OWNER/REPO
   codex plugin add agents-over-telegram@agents-over-telegram
   ```

   These are Codex CLI commands. Local marketplace discovery and installation have been checked; installation from a published GitHub repository remains untested.
2. **Prepare Telegram.** Create a bot with `@BotFather` if you do not already have one for this setup, and add it to a dedicated group or forum topic. Reuse that bot for later agents. Keep its token private; the wizard asks for it in a hidden terminal prompt. Direct-message pairing is not supported.
3. **Open the intended project in Codex and ask “Connect this agent to Telegram.”** The setup skill prepares the runtime from the installed plugin and gives you the private pairing command. Run the terminal wizard, send its exact `/pair@YourBotUsername …` command in the intended group/topic, then confirm the observed destination in the terminal. First registration also confirms that this bot has no other receiver. Adding another project reuses the registered bot without entering its token again.
4. **Load the connection.** Open/add the prepared project, make Codex's normal project-trust decision, then reload/reopen its chat. Ask **“Telegram status”** to verify the actual bot and destination through the loaded tools.
5. **Choose when to listen.** Ask **“Listen for Telegram in this chat for 10 minutes and reply to my messages.”** Keep ChatGPT and the listener running for that period. Open ChatGPT from the app list as usual. Automatic wakeup is currently verified only on Linux with bundled Codex **0.163.0-alpha.10.2**.

**Every chat in a project shares its Telegram identity.** Use separate project directories for separate agents. Setup writes one native project `telegram` MCP entry and a profile reference; it does not configure a global plugin MCP server or change global trust. The runtime stays outside the plugin cache, so a cache refresh does not remove the configured executable. Existing configured projects keep their current runtime during ordinary setup.

For the terminal commands, offline bootstrap and existing-account options, see [local setup](docs/OPERATIONS.md#codex-setup).

## Everyday use

Ask Codex:

- **“Telegram status”** — show the actual bot, destination, pause state, backlog and next action.
- **“Pause Telegram” / “Resume Telegram”** — control this project's connection.
- **“Show recent Telegram context”** — read captured messages from the last 24 hours, including handled messages.
- **“Send Beta this testing result…”** — send within the recipient and purpose you authorize.
- **“Listen for Telegram”** — start a bounded listener for a chosen local chat while ChatGPT is open normally.

Status and history do not contact Telegram or send messages. Collection is **on demand** by default: asking for new messages polls the shared collector and may progress previously authorized queued sends. Setup starts no listener or background service.

The optional [desktop listener](docs/DIRECT_DISPATCH.md) uses native Codex queueing and a wake URL to notify an existing local chat. **Open ChatGPT from the app list as usual.** No custom app launcher, protocol wrapper or relay model is required. Listening is explicit and bounded; the target uses its own scoped Telegram tools to handle pending work.

On Linux with bundled Codex **0.163.0-alpha.10.2**, the live `relay-desktop` check passed with ChatGPT opened normally: Telegram marker `normal-idle-1` woke the existing local Alpha chat, which polled once, sent one reply and acknowledged the delivery after publication was recorded as sent. No model forwarded the wake after the marker arrived, and Alpha returned to zero pending messages. Native tool and ledger evidence confirm the round trip; the user also confirmed the reply is visible in Telegram. The [compatibility record](docs/COMPATIBILITY.md) separates this result from earlier relay and shared-backend experiments.

History covers captured local records, not a complete prior-day Telegram transcript. Messages consumed elsewhere or missed before binding and the old bridge's archive are not imported.

## Validation and setup details

The first-run check passed from a clean local Codex CLI installation: bootstrap ran from the installed plugin cache, reused the same runtime on repetition, and the runtime still worked after both source and plugin-cache copies were moved away. Fresh fake Alpha/Beta setup and scoped native MCP checks passed. The full suite passed **534 tests in 21.15 seconds**. Earlier live Telegram checks also covered peer delivery, pause/resume and normal-desktop wake/reply/ack. Published GitHub installation, the desktop plugin UI and its onboarding button remain untested. See [implementation status](docs/IMPLEMENTATION_STATUS.md) for the evidence and limits.

- [Guided setup, local controls and advanced CLI](docs/OPERATIONS.md)
- [Listen with the normally opened desktop](docs/DIRECT_DISPATCH.md)
- [Shared binding procedure and older model-mediated relay](docs/RELAY.md)
- [Compatibility and scope boundaries](docs/COMPATIBILITY.md)
- [Live validation and deliberate receiver migration](docs/LIVE_VALIDATION.md)
- [Implementation and validation evidence](docs/IMPLEMENTATION_STATUS.md)
- [Original proposal](PROPOSAL.md) and [handoff](HANDOFF.md)

## Offline development

Python 3.11+ is required; Linux with Python 3.13.7 was tested. From the source root:

```sh
uv sync --frozen --extra dev --python /usr/bin/python3.13
.venv/bin/aot demo
.venv/bin/python -m pytest -q
.venv/bin/python scripts/validate_package.py
```

The demo and automated tests use isolated fake state. They need no token and do not call Telegram. The lockfile pins dependencies, including the official Python MCP SDK. Source/runtime installation does not install a daemon or select a live account. Claude integration is deferred; this workflow targets Codex.
