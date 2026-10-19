# Implementation handoff

## Current continuation: 0.5.1 direct dispatcher

The local MVP and the bounded 0.4.0 model-mediated relay have been implemented and tested. The current optional integration is documented in [DIRECT_DISPATCH.md](docs/DIRECT_DISPATCH.md): `relay-dispatch` joins the desktop's deliberately shared backend, submits fixed tool output for an authorized mapped thread and retains separate notification/processing/publication outcomes. `aot-codex-bridge` preserves the exact desktop stdio owner while adding a private Unix WebSocket. Setup does not enable it automatically.

An isolated bundled-runtime protocol check proved independent idle wakeup, busy-turn tool-output delivery, owner handling of dynamic tools/approvals and completion after dispatcher disconnection. A manually launched bounded dispatcher on the actual desktop's shared backend also passed a live Telegram idle wake/reply/ack check, with zero model-forwarded target messages after the Telegram injection. The 0.5.1 fix handles the real UI's lack of an `initialized` notification by gating child startup on its matching successful initialization response instead. Automatic startup still needs one final GUI restart check; a longer real busy-target test is also pending. Do not conflate manually launching the dispatcher process with forwarding each event through a model, or start a second app-server over the desktop's saved history. Read [implementation status](docs/IMPLEMENTATION_STATUS.md) for current test results. The original handoff below describes the initial isolated build and its receiver boundary; it is preserved as historical context.

## Original implementation handoff

The user requested this proposal in `/home/daniele/Documents/agents-over-telegram` and explicitly asked to start a separate chat with an agent that can work on it. The latest clarification is mandatory: **one bot per human, reused by multiple agents in dedicated conversations; setup only once**. Read PROPOSAL.md before implementing.

## Authorized work

Build and validate the local MVP here: reusable core, isolated/fake transport tests, guided setup, MCP adapter, plugin packaging and installation documentation. Make routine implementation choices autonomously. Keep the implementation focused and record evidence/limitations. A local Git repository may be initialized here if useful; no remote repository, publication or deployment has been requested.

Use this directory even if the new chat initially opens in a generic projectless workspace. There is no registered Codex project for this new directory yet. The user explicitly authorized creating and working in this folder; use normal scoped filesystem permission requests if the host requires them. Do not move implementation into Remozio or change Codex's internal project database.

## Existing prototype: source reference only

The private source at `/home/daniele/.local/share/remozio-agent-telegram/bridge.py` may be inspected read-only for implementation ideas. It is approximately 150 lines of standard-library Python. It already has explicit poll/ack/send operations, a mutex, durable inbox/outbox state, file-and-directory fsync, duplicate checks, send limits and sanitized network errors.

Its limits matter: hardcoded private directory, one group filter, one bot-wide cursor, globally acknowledged pending messages, no pagination/recipient leases, and only attempted/sent outbox states. Copying this poller per agent will lose or compete for updates. Its blanket own-bot-message ignore also needs different treatment for multiple logical agents sharing a bot.

Do not read/copy the existing token, config, state or messages. Do not run the live bridge, consume getUpdates, change webhooks, install over it, or send test messages through its account. The parent Remozio chat owns that active bot and monitoring workflow. Build with fake credentials/transport and a separate test state root. Include a future opt-in test/migration procedure rather than disrupting that integration.

## Research starting points

Official sources verified on 2026-10-19 are linked in PROPOSAL.md. Recheck current plugin and MCP SDK schemas before implementation; the proposed tool names are not existing APIs. Use OpenAI Docs for host-specific plugin questions and the skill-creator instructions when authoring the bundled skill. Public plugin publication and automatic local wakeups remain unvalidated.

Keep platform claims separate from design decisions. Test on this Linux host first; mark macOS/Windows or live Telegram behavior untested until actually exercised. Prefer a small maintainable implementation over a new generic agent orchestration framework.

## First concrete deliverable

Implement and test the durable one-bot/two-agents routing core, then wire it into a minimal MCP server and local plugin. Include setup instructions, test commands and an implementation status document. Continue until the local MVP and meaningful offline tests are working, or report a concrete blocker with preserved work. No need to request the already-supplied live bot token: it is deliberately out of scope for this isolated build.

