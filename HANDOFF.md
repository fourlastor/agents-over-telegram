# Implementation handoff

## Current continuation: 0.7.2 public release

The committed 0.6.0 checkpoint is `0a6db68`. The preferred listener is `relay-desktop`: ChatGPT opens normally, and native queueing plus its wake URL starts an existing local chat. The live Telegram round trip and duplicate check passed. Do not resume custom-launch or replacement-backend work as the default path.

Version 0.7.2 targets the authorized public repository `fourlastor/agents-over-telegram` under Apache-2.0. Publication and fresh remote-install verification remain pending. The listener's exact-version gate is replaced by read-only native queue capability checks; other Linux process/schema/routing guards remain intact. The tested build remains 0.163.0-alpha.10.2, without claiming future-build or other-platform live proof. The release suite passed **572 tests in 21.71 seconds**; 73 focused queue/dispatcher checks and 79 license/bootstrap/package/requirements checks passed. Real offline pip bootstrap and canonical license inclusion in wheel, source archive and plugin ZIP passed.

Version 0.7.1 removes uv as a first-time prerequisite. The default bootstrap chooses uv when available, otherwise Python 3.11+ venv/ensurepip and pip inside the private runtime. The pip path pins and hash-checks both runtime and build dependencies; the uv path retains its existing frozen runtime resolution. Stable runtime paths remain unchanged. Explicit installer selection and a populated wheelhouse support offline pip installation. It does not use global pip or install uv automatically. The real online uv-free first-run check passed through native CLI install, pip bootstrap, repeat reuse, source/cache removal and fresh fake scoped MCP onboarding. The offline pip path also passed with a 35-wheel compatible wheelhouse, and the existing uv path passed a fresh offline bootstrap. The full 0.7.1 suite passed **561 tests in 21.95 seconds**, including 39 bootstrap tests. Keep previous-release evidence distinct.

Version 0.7.0 adds the repository marketplace, a bundled bootstrap that creates a stable runtime outside plugin caches, updated setup guidance and accurate listener-status wording. Native CLI installation, cache-independent runtime preparation and fresh fake-project MCP onboarding passed; 534 automated tests passed. GitHub transport and desktop plugin UI/onboarding still need a real repository and UI acceptance check. At 0.7.0, no remote repository had been created or published. Read [implementation status](docs/IMPLEMENTATION_STATUS.md) for the current evidence.

## Historical 0.5.1 direct dispatcher

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
