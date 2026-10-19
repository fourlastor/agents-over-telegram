# Agents over Telegram

Connect a Codex project to a Telegram group or forum topic. Register **one bot once**, then reuse it for multiple logical agents in separate project conversations. Telegram shows the actual shared bot; messages label the logical agent clearly.

## Connect from Codex

Install the Python runtime first (Python 3.11+), from a stable source checkout:

```sh
uv sync --frozen --python /usr/bin/python3.13
```

Use an available Python 3.11+ interpreter path on another host. The plugin cache does not provide an installed `aot` runtime; keep the checkout and its virtual environment available.

With the plugin installed, ask **“Connect this agent to Telegram.”** Its `telegram-setup` onboarding skill guides the local setup. Normal installation adds the setup and coordination skills. Setup then creates one native `telegram` MCP entry in the chosen project's configuration. No global plugin MCP server or `AOT_*` launcher environment is needed. The bot token stays in a hidden terminal prompt and never belongs in the chat.

The setup command is also available directly from this source checkout's installed environment:

```sh
"/home/daniele/Documents/agents-over-telegram/.venv/bin/aot" setup --label Alpha --project-dir "/absolute/path/to/alpha-project"
```

Choose the actual project path. The wizard reuses the registered bot, pairs the project with its destination, and prepares its local Codex configuration. Add `--state-dir /actual/shared/state` before `setup` if the account already uses a different state directory. For first registration, the wizard asks you to confirm that the selected bot has no other receiver before its hidden token prompt. You can supply that assertion with `--receiver-handoff-confirmed`; adding agents to an existing registration needs no repeat token entry. Keep every agent for that bot on the same state directory and host.

**Every chat in that project shares its Telegram identity.** Use separate dedicated project directories for separate agents. Open/add the prepared project in Codex and make the normal project-trust decision yourself. Reload/reopen the project chat so Codex loads its native MCP entry, then ask for Telegram status to verify the actual bot and destination. Plugin setup does not change global trust settings.

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

Live checks on 2026-10-19 exercised Codex's scoped tools, same-bot peer delivery and duplicate suppression, and Alpha's pause/queued intake/resume/reply/ack flow while Beta remained usable. Installing this package through the Codex plugin UI and its onboarding button remain untested. The 0.3.1 installation wiring correction and its validation status are tracked in [implementation status](docs/IMPLEMENTATION_STATUS.md). Earlier runtime tests do not establish clean-machine installation or plugin UI behavior.

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
