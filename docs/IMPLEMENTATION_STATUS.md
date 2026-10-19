# Implementation status and validation evidence

Date: 2026-10-19. Version: 0.7.1. Source root: `/home/daniele/Documents/agents-over-telegram`.

**Working local implementation with offline tests and scoped live checks.** The automated suite uses one fake bot reused by two isolated clients. Separate live checks on 2026-10-19 exercised native Codex tools and the shared-bot workflow described below. Native CLI marketplace installation, runtime bootstrap and fresh-project MCP onboarding have been tested in isolated profiles. Published GitHub retrieval, desktop plugin UI installation and automatic onboarding invocation remain untested. No background service is installed by this package. The original proposal and handoff are preserved.

## Implemented

| Area | Result |
| --- | --- |
| Shared account | One immutable bot identity per private state directory, reused by every client; no per-agent bot token. |
| Private setup | Hidden terminal token input, read-only `getMe`/webhook validation, refusal of existing webhooks, explicit receiver-ownership assertion, protected credential file. Tokens are not MCP arguments. |
| Pairing | Expiring single-use addressed code; observed human numeric sender and exact group/topic reviewed and confirmed through the operator CLI. No confirmation MCP tool. |
| Client boundary | Random capability hashed in SQLite; protected client bootstrap file pins each stdio process. Revocation is checked on every tool operation. |
| Intake | One collector lock and bot-wide durable cursor per shared state root. All configured destinations route within one SQLite transaction before cursor advance. Replay is idempotent. |
| Inbox | Binding-scoped deliveries, bounded pages, recoverable leases, explicit acknowledgment; another binding cannot claim or acknowledge them. |
| Recent history | Read-only `messages_history` and CLI `history` expose the connected client's captured last 24 hours inbound/acknowledged/outbound context, with provenance and live delivery status. Signed client-bound snapshot pagination expires after 15 minutes and never claims, acknowledges or resends work. |
| Outbox | Intent persisted before send; key scoped to client; content mismatch rejected; queued, sending, sent, rejected and unknown states. Same intent reuses its existing record. |
| Recovery | Interrupted sending becomes unknown after the sender lock is available. Unknown is never automatically retried; operator accepts, cancels or explicitly queues a retry. |
| Shared-bot coordination | Owner-granted directional peer routing inserts one correlated broker delivery atomically with the outbox. Publication outcome is exposed independently; no own-bot Telegram echo is needed. |
| External bots | Numeric sender allowlist plus per-binding configurable addressed peer command; actual live visibility remains untested. |
| Controls | Per-bot, destination and conversation pacing; bounded conversation send counts; owner pause command; binding pause/disconnect; edited-message events and suspension on removal/migration/topic changes. |
| MCP/plugin | Eleven scoped tools using official Python MCP SDK 1.30.0, explicit schemas and annotations, structured results and sanitized wire/validation errors. Skills-only portable and compatibility plugin manifests; setup creates one native project MCP entry. An explicit advanced renderer generates a private one-client MCP package. |
| Skill/docs | Codex onboarding skill and routine status/pause/resume/history/coordination guidance, credential and lifecycle instructions, source/schema evidence and deliberate receiver handoff/rollback procedure. |
| Optional relay prototype | Exact-project client-to-thread mapping, bounded pending-inbox watcher and separate wake-notification outcomes. A running Codex chat performs the native host handoff; one bounded live idle-target wake/reply/ack test passed. Setup starts no listener. |
| Desktop listener | Native queue plus desktop wake URL wakes an existing local chat while ChatGPT opens normally; live Telegram poll/reply/ack passed. Earlier shared-backend launch experiments remain secondary. |
| First-time runtime | Bundled bootstrap prepares a stable runtime from the installed plugin; native CLI marketplace install and fresh fake-project MCP onboarding passed. Python 3.11+ is required; uv is optional, with a venv/ensurepip and private-pip fallback. |

## Version 0.7.1: optional uv

First-time setup no longer requires uv. The bootstrap defaults to `--installer auto`, selecting an existing uv installation when available and otherwise using Python's `venv`/`ensurepip` plus pip inside the private runtime. Explicit `--installer uv` and `--installer pip` modes support operator choice. Providing `--wheelhouse` selects pip in auto mode; combining it with explicit uv is rejected. The receipt reports the installer used, and reuse preserves that original receipt. The required interpreter remains Python 3.11+; the fallback needs its virtual-environment and pip bootstrap support. No global pip installation, automatic uv installation or change to Telegram authorization is introduced.

The pip fallback pins and hash-checks both runtime and build dependencies and accepts compatible wheels only. The existing uv path retains frozen runtime lock resolution; its isolated build dependencies do not gain this new transitive hash pinning. The shipped `requirements/runtime.txt` and `requirements/build.txt` are generated by `scripts/export_requirements.py` from the locked repository metadata. A new uv offline build uses its cache; a new pip offline build requires `--wheelhouse` with compatible runtime and build wheels. A valid existing runtime is reused, and configured projects retain their selected executable.

The real online pip check passed with no uv executable available (`PATH=/usr/bin:/bin`). Native Codex CLI installation in isolated `HOME`/`CODEX_HOME` produced a plugin cache copy; default bootstrap selected pip, installed 0.7.1 and reused it on repetition. Moving both source/cache package copies away left runtime help, imports and `pip check` working. Fresh fake Alpha/Beta pairing, scoped native MCP, read-only status, pause/resume and retained history also passed. No live credentials, real Telegram calls or existing project configuration were used.

The test interpreter was full CPython 3.13.7 with ensurepip, already installed under `~/.local/share/uv/python`. That location records its provenance; the run used neither a uv executable nor a uv dependency cache and downloaded no interpreter. A separate system interpreter lacking ensurepip produced the actionable missing-venv diagnostic. Evidence: `outputs/uv-free-smoke.json` in the implementation workspace.

A fresh offline pip install and the same scoped MCP checks also passed using a 35-wheel compatible wheelhouse with network index access disabled. The full 0.7.1 suite passed **561 tests in 21.95 seconds**, including **39 bootstrap tests**. A fresh 0.7.1 bootstrap through the existing uv path also passed with `--installer uv --offline`. The offline pip run used a clean runtime and installed plugin, `--no-index`, and the populated compatible wheelhouse; it exercised the same fresh fake scoped MCP flow. Evidence: `outputs/uv-free-offline-smoke.json`. These checks did not publish a GitHub repository, exercise the desktop plugin UI or use live Telegram. The 0.7.0 evidence below remains a separate historical record.

## Version 0.7.0: first-time installation and runtime setup

The repository includes `.agents/plugins/marketplace.json` with one root plugin. The installed native Codex CLI successfully registered this catalog and installed `agents-over-telegram@agents-over-telegram` into a disposable Codex home. The same CLI supports GitHub `OWNER/REPO` marketplace sources. No remote repository has been selected or published, so GitHub download and desktop UI installation are not claimed as tested.

Version 0.7.0 introduced runtime preparation directly from the installed plugin using `scripts/bootstrap_runtime.py`; that version required existing Python 3.11+ and uv. It copies only the runtime sources and locked package metadata into a private directory selected by package contents and interpreter identity, installs non-editably, validates the executable and returns its absolute path. Plugin cache refreshes do not invalidate that executable. Matching successful installations are reused; interrupted installation can be retried, and older runtimes remain available. Existing configured projects keep their selected executable instead of silently migrating on a plugin update. No credentials, project configuration or listener are created by bootstrap.

Status now distinguishes the inbox core's on-demand behavior from a desktop listener whose status has not been checked. Pairing guidance explains adding the actual bot to a dedicated group/topic, and completion points to native tool verification and an explicit listening period.

Validation on 2026-10-19:

- **534 automated tests passed in 21.15 seconds**, including 25 bootstrap tests; package and setup-skill validation passed.
- Real uv installed the locked dependencies into a fresh temporary runtime. Repeated offline setup reused it. A fresh offline installation also passed with a long runtime path containing spaces.
- An end-to-end check used a disposable HOME/CODEX_HOME to install the plugin through the native Codex CLI, bootstrapped the installed cache copy, and removed both package locations from their original paths. The runtime continued to work and imported its installed code from its own virtual environment.
- That new runtime prepared and paired two fake projects, launched both generated native MCP commands, verified their separate identities, read-only status, pause/resume and history. Setup reruns preserved profile/configuration bytes. An unconfigured child project did not inherit its parent's identity. No live Telegram, real credentials, existing project settings or live Codex profile were used.

Evidence in the implementation workspace: `outputs/first-run-smoke.json`. For that 0.7.0 release, the local plugin/UI was not a portable Python installer: Python and uv were prerequisites, private token entry and destination review remain terminal steps, project trust/reload remains a host step, and listening remains explicitly started. No custom ChatGPT startup, background service or autostart entry was added.

## Version 0.6.0: normal desktop wake

The preferred listener is `relay-desktop`, using native queueing and the existing desktop wake URL while ChatGPT opens normally. Live Telegram marker `normal-idle-1` woke the existing local Alpha chat and produced exactly one reply followed by acknowledgment; the user confirmed it in Telegram. The quiet check showed no duplicate turn. Version 0.6.0 passed 502 tests. This route supersedes the custom-launch experiments retained below; see [compatibility](COMPATIBILITY.md#normal-launch-local-desktop-boundary) for precise scope and receipts.

## Version 0.5.1: desktop startup compatibility and live direct validation

The actual desktop attached successfully to the shared backend, but its main UI does not send an `initialized` notification. Version 0.5.0 waited for that notification, so the configured dispatcher child never started. Version 0.5.1 accepts the matching successful initialization response after it has been forwarded to the UI, while retaining compatibility with the explicit notification path. The full regression suite passed **438 tests in 18.89 seconds**, including **61 desktop bridge tests**. Fixtures isolate inherited `AOT_*` launcher settings so fake-endpoint tests cannot select the live dispatcher's state.

A startup-fix integration variant passed **15 checks** with the actual bundled backend and a simulated owner that sends no `initialized` notification. It observed exactly one automatically spawned dispatcher ready event, idle wakeup, a matching receipt, owner tool handling, no extra model activity beyond the expected three requests, and process/socket cleanup. Evidence: `work/direct-probe/startup-fix-result.json`. This validates the corrected initialization path against the real backend; the actual GUI still needs its final restart check.

A live direct idle-target check passed on 2026-10-19 using a bounded dispatcher process launched manually on the desktop's existing shared backend. “Telegram Alpha desktop test” completed its preparation turn and was idle with its native connection identity verified before the human sent `direct-idle-1`. The dispatcher captured incoming Telegram message **20** and submitted notification `wake_cc87bc0117117a3490e4c36da0a84ffd`, accepted on attempt **1**. No model sent the target another chat message after the Telegram injection. The target began turn `01a15468-f735-7312-b7c5-8b44d61083d6`, used native `messages_poll`, `message_send` and `messages_ack`, replied **“Direct idle test passed”** as Telegram message **21**, and acknowledged the delivery after recording the send. The turn completed in **42.315 seconds** and pending inbox count returned to **0**.

Independent ledger evidence records the accepted notification, acknowledged delivery `msg_fe8cf270090e7becbcbcaee9`, and sent outbox row `out_1eb9f23ee50f204a612010a2` under idempotency key `direct-idle-1-reply`. The investigation artifact `outputs/direct-live-receipts.json` records these receipts and zero post-injection parent messages. Native desktop app tools remained available. This proves an actual direct Telegram-to-idle-Codex-to-Telegram round trip with a manually started dispatcher; the manual process launch is not model-mediated event forwarding.

Automatic dispatcher startup through the corrected 0.5.1 gate still requires one final actual GUI restart check. A longer busy-target/tool test on the real desktop also remains pending. The isolated protocol and simulated-desktop coverage below support those primitives but do not substitute for these remaining host checks. The previous 0.4.0 live result remains a separate model-mediated relay test.

## Version 0.5.0: optional direct dispatcher

The [direct dispatcher](DIRECT_DISPATCH.md) reuses the existing client/thread bindings and notification ledger. It validates the returned target UUID and project directory plus fresh local profile/client/MCP configuration, then submits `turn/start` with empty input, fixed `toolOutput` and `turnTrigger: "telegram"`. It does not change model, working directory, permissions or approval policy. It never claims or acknowledges inbox messages, flushes sends, or answers/declines app-server approval and tool requests. Backend acceptance remains separate from turn completion, Telegram publication and inbox acknowledgment; ambiguous submissions are held without blind retry.

The optional `aot-codex-bridge`, selected through `CODEX_CLI_PATH`, wraps the desktop's exact bundled executable, arguments and environment. It preserves the stdio owner and exposes a private Unix WebSocket to the dispatcher. A configured child can start after desktop initialization and stop with the bridge; normal setup installs no service and does not enable this mode. The bundled raw `app-server proxy` cannot substitute for this WebSocket framing adapter.

An isolated protocol test passed with `/usr/lib/chatgpt/resources/codex` **0.163.0-alpha.10.2**, a clean temporary `CODEX_HOME`, private Unix WebSocket and local fake Responses provider. An independent client resumed an idle thread and started generation; the original owner received lifecycle events. A busy submission returned the existing turn ID and delivered tool output within that turn. Tool output reached the provider as a function-call output rather than user text. Dynamic tool and command approval requests reached both subscribed clients, and the original owner could resolve the tool and decline the approval while the dispatcher remained passive. An accepted turn completed after dispatcher disconnection with the owner still attached. Evidence is in the investigation artifact `work/direct-probe/protocol-summary.json`; these are backend protocol checks, not a real desktop/Telegram test.

Final 0.5.0 regression: **423 tests passed in 17.04 seconds**, including 62 app-server client tests, 33 dispatcher tests and 46 desktop bridge tests. Package schema validation and Python compilation passed.

An additional integrated check passed all **14 assertions** using the real bundled backend, a simulated desktop connected through the actual bridge, its automatically started dispatcher, fake Telegram state and a local fake model. Injecting one Telegram update woke the idle target without any owner wake request. The owner handled a dynamic tool call, the durable accepted receipt matched the completed turn, the inbox remained unleased/unacknowledged and queued outgoing work remained unchanged. Duplicate intake and quiet waiting created no extra model turn. Closing desktop stdin stopped the owned backend/dispatcher and removed the socket. Evidence: `work/direct-probe/integrated-result.json`. The custom Unix endpoint layout and peer ownership were also checked against the actual bundled runtime.

The later 0.5.1 validation above established actual desktop attachment and a live direct Telegram round trip with a manually launched dispatcher. It also found and corrected the real UI startup-notification difference; automatic startup after that correction remains pending a final restart check. The live 0.4.0 result below used a model-mediated relay and must not be reported as direct-dispatch validation.

## Version 0.4.0: optional relay prototype

The [relay workflow](RELAY.md) adds an explicit “Listen for Telegram” mode without changing normal on-demand use. It reuses the shared collector and durable inbox, emits a fixed metadata-only notice, and leaves lease/reply/ack handling to the target's existing scoped tools. Binding defaults to a baseline at current pending work; `--include-pending` opts into that backlog. Notification acceptance is recorded separately from inbox handling and Telegram publication. Each event revalidates the exact project's profile, client, state, mode and native MCP entry; drift blocks notification without consuming pending work. Only enabled mappings reserve a target UUID, and local operator status/unbind remain available for a revoked client without restoring its transport authority.

The local watcher cannot call `send_message_to_thread` externally. An already running Codex relay chat must use that native host tool for an explicitly authorized target, verify the target's exact project and resolve the notification after the call. The recommended shell path is `relay-watch --once --duration 55`, reissued within the chosen listening period. Persistent mode requires attached stdin and an explicit stop; duration bounds also apply. Parent-process checks cannot determine that a Codex chat completed or was deleted. Zero model activity while idle is not claimed.

The [third-party relay report](https://gist.github.com/rock3r/60760bdf3c722def30d57a988ed88bbe) describes an active-target round trip. It is not validation of this implementation, an idle-target wakeup, or the relay's lifecycle in this host. The full regression suite passed **282 tests in 4.78 seconds**, including **25 focused relay-store cases**. Package and skill validation also passed. Tests use offline transports; the existing MCP subprocess tests ran outside the sandbox after the sandboxed run stalled. A bounded live idle-target acceptance check also passed, as detailed below. These results are separate from the earlier versioned runtime checks below. No daemon, scheduler, alternative app-server or MCP Events integration is installed by this prototype.

The live 0.4.0 check started with “Telegram Alpha desktop test” idle. A human sent `relay-idle-1` in its bound group. The bounded collector/watcher emitted exactly one notification and exited. The running relay chat forwarded its fixed notice with explicit, human-authorized test instructions using native `send_message_to_thread`. Alpha started a new turn, verified its scope and called native `connection_status`, `messages_poll`, `message_send` and `messages_ack`. It polled once, replied “Relay idle test passed” to incoming Telegram message 18 as outgoing message 19, acknowledged only that delivery, and returned to idle. The private ledger independently recorded one accepted notification, one acknowledged incoming delivery and one sent outbox row under the stable test idempotency key. A post-test local wait emitted no duplicate notification. No watcher was left running.

This validates the session-scoped relay path for that idle target. It does not establish an unattended service, a completed relay chat being woken by process output, zero model activity during idle waits, or the busy-target path under this new relay. The host-mediated test included explicit instructions authorizing its one reply; a generic notice does not grant new outbound authority.

## Version 0.2.0: initial 24-hour history

The `messages_history` MCP tool and CLI `history --client-file PATH` read recent captured context, including acknowledged inbound messages and outgoing/broker publication state. Each response explicitly identifies its coverage as `captured_only` and its content as context, not new queued work. The initial request fixes a 24-hour event-time window and row membership; signed scope-bound cursors page newest first for 15 minutes. New inserts do not shift an existing snapshot. Acknowledgment/publication states remain current as of each page read.

Telegram events use valid message/edit timestamps; legacy or missing/invalid timestamps fall back to capture time. Outgoing and broker events use their local creation time. Older pending work remains durable and claimable through normal polling even when absent from the 24-hour history view. This is a read window, not automatic deletion.

History performs no network calls or lease/ack/send transitions. The private history CLI also disables startup send recovery, so a `sending` row stays unchanged while inspected. Normal startup and send paths keep their existing interrupted-send recovery behavior. Schema initialization may still apply a local database migration.

This does not provide a complete 24-hour Telegram transcript: previously consumed updates, prebinding/unconfigured bodies and old-bridge state are not imported. The history feature does not import or change another bridge's receiver permissions.

## Version 0.3.0: Codex setup and controls

The 0.3.0 workflow adds `telegram-setup` as the declared onboarding skill, natural Codex prompts for routine controls, guided local setup, exact-project profile selection and safe status metadata. Every chat in one configured project shares its client identity. Project trust remains a user decision. There are no automatic wakeups, and Claude integration is deferred.

Both manifest formats point to the included onboarding skill. Package tests cover missing onboarding content, invalid/traversing paths, symlink escape and compatibility mismatch. Skill validation checks both entrypoints. The 0.3.0 source distribution and wheel built successfully; the wheel includes the new onboarding module. The build fetched its declared build dependency after an offline cache miss. Application tests and the smoke scenario used no network. The full 0.3.0 suite passed: **238 tests in 2.84 seconds**, including 31 new setup/profile cases, 13 new status/privacy cases and seven additional package checks. A separate offline smoke test prepared two projects, paired them, launched both generated MCP commands through the official SDK, and verified scoped status, pause/resume and history. Reading status left the fake transport unchanged. Tests used temporary fake state and no Telegram calls; subprocess checks ran outside the execution sandbox after its MCP subprocess launch stalled.

## Version 0.3.1: installation wiring correction

Normal plugin installation now packages setup and coordination skills without a root `mcp.json`, `.mcp.json` or compatibility `mcpServers` declaration. Guided setup creates the single native project `telegram` MCP entry. This removes the normal installation's separate launcher, which previously required profile environment settings that onboarding did not supply. Existing project tools could mask that failure.

The cached plugin still requires an independently installed Python `aot` runtime. Setup records its absolute executable and scoped state/client paths, then the user reloads/reopens the project chat to load those native tools. All chats in the exact project share that identity. Disabling the skills plugin leaves the native entry configured; removal is documented in [operations](OPERATIONS.md#pause-disconnect-and-uninstall). No automatic runtime bootstrap, background collection or wakeup is added.

Advanced `plugin-render` remains available and explicitly synthesizes a private one-client MCP package. It must not be globally enabled or combined with the normal native entry. It is not part of the default installation path.

The full regression suite passed: **245 tests in 2.91 seconds**. A follow-up renderer correction removes its redundant onboarding action; all 24 focused CLI/package tests passed after that change. Package validation and both skill validators passed. The reusable `scripts/smoke_onboarding.py` also passed against the installed 0.3.1 runtime with all `AOT_*` settings cleared: it packaged/extracted the normal plugin, prepared two empty projects, paired fake destinations, launched their newly generated native MCP commands, checked isolated pause/resume/ack/history and rejected identity inheritance in an unconfigured child project. Setup reruns preserved configuration and profiles byte for byte. No Telegram calls or existing project changes were made. This uses an already installed runtime; it is not a clean-machine bootstrap test. Actual plugin UI installation and onboarding invocation remain pending. The earlier counts below describe 0.3.0 and 0.2.0.

## Live checks on 2026-10-19

The owner-driven session exercised native scoped Codex tools against Telegram: same-bot peer broker delivery, repeated-key duplicate suppression, incoming work retained while Alpha was paused, and Alpha's resume/reply/ack flow while Beta remained usable. These checks support that selected configuration. They do not prove every privacy/administrator configuration, external-bot interoperability, network-failure recovery, or the Codex plugin UI installation/onboarding path. These earlier tests did not demonstrate an unattended collector or local chat wakeup. The separate 0.4.0 relay check below demonstrated one idle-chat wakeup while its relay owner remained running.

## Executed validation

Host: Linux, CPython **3.13.7**. Runtime dependencies are declared directly and exact versions are in `pyproject.toml`; all resolved dependencies are pinned in `uv.lock`. Python 3.11+ is declared, but only 3.13 was executed here.

The prior 0.2.0 full suite completed with **187 passed in 2.47 seconds**:

| Tests | Count | Evidence |
| --- | ---: | --- |
| Core | 38 | Interleaving, replay/restart, atomic rollback, collector and lease concurrency, pairing misuse/expiry, wrong senders, broker grants/provenance, rate/chain limits, rejection/429/unknown, queue bounds, edits, migration/removal, pause/revocation and persistent offline-gap reporting. |
| MCP | 34 | Official SDK initialization/tool discovery, schema validation, bounded structured errors, malformed-wire secret suppression, two simultaneous actual stdio client processes, isolated queues/acknowledgments, broker delivery, restart, pause and disconnect. |
| History core | 41 | Valid Telegram/event timestamps, exact 24-hour cutoff, 23-hour available backlog, 25-hour context exclusion without dropping pending work, edits, legacy-schema migration, ACK/restart preservation, pagination stability, signed cursor integrity/expiry and read-only behavior. |
| History adversarial | 7 | Cross-client cursor rejection, tied timestamps across streams, late-insert exclusion, revocation/rebinding isolation, frozen snapshot/expiry, absent prebinding history and broker context. |
| Adversarial | 35 | Credential permissions/ownership checks, symlink/hardlink refusal, HTTP/body/timeout diagnostics, malformed MCP request/notification logging, forged labels, lease isolation, unknown-send recovery. |
| Transport and credentials | 18 | Private file writes, fixed errors, HTTP rejection/rate-limit/unknown classification, malformed send responses, plain-text send shape, persistent fake acceptance without echo. |
| CLI | 6 | Register once/create two clients/pair/confirm/peer configuration, pause/disconnect, fake/live state mismatch, mandatory receiver handoff assertion, schema-valid secret-free profile rendering and offline demo. |
| Actual process crashes | 3 | `os._exit` before intake transaction commit, after commit, and after fake remote acceptance but before saving send confirmation. Recovery preserved work/cursor and prevented automatic duplicate publication. |
| Plugin package | 5 | Vendored portable schema validation, compatibility parity, local paths and fail-closed launch configuration. |

The earlier 0.2.0 run also passed these checks:

- `scripts/validate_package.py`: portable schemas, compatibility parity and referenced local component paths.
- The bundled skill-creator `quick_validate.py`: skill frontmatter/name/scaffold checks.
- Independent skill trials: account reuse, forged peer/exfiltration instructions, acknowledgment disposition, and unknown-send recovery boundaries.
- `uv build`: source archive and Python wheel built successfully.
- A new runtime-only virtual environment installed the wheel without development dependencies; `aot demo` passed and the MCP adapter imported **11 tools**.
- The demo reported one bot, two agents/topics, denied cross-agent acknowledgment, broker origin, duplicate suppression, unknown-send preservation across restart, pause isolation below queue capacity, acknowledged history, isolated pagination, and a verified 24-hour cutoff. It used temporary state and no token or network.

Public package dependencies and official documentation/schema files were fetched during implementation. The application tests themselves used fake transports and isolated state; they did not call Telegram. A first clean-install attempt using only the local dependency cache was incomplete, so the missing public package wheels were fetched before the successful clean-install check.

## Reproduce

From the source root, with `uv` and a Python 3.11+ interpreter available:

```sh
uv sync --frozen --extra dev --python /usr/bin/python3.13
.venv/bin/python -m pytest -q
.venv/bin/python scripts/validate_package.py
.venv/bin/python scripts/smoke_onboarding.py --aot "$PWD/.venv/bin/aot" --output /tmp/aot-onboarding-smoke.json
.venv/bin/aot demo
uv build --out-dir work/dist
.venv/bin/python scripts/package_local.py outputs/agents-over-telegram-0.5.1.zip
```

`--python /usr/bin/python3.13` identifies the actual interpreter tested here; adapt the path on another host. Initial dependency installation may need network access. The existing `.venv` in this checkout is ready for the offline test and demo commands.

The local source-plugin ZIP contains an explicit allowlist of source, tests, manifests, skill, scripts and documentation. It excludes the virtual environment, local state and credentials. It installs the setup and coordination skills. The setup skill can prepare a stable runtime from the installed package and then run guided setup to create the native project MCP entry, as documented in [operations](OPERATIONS.md). A clean wheel supplies the runtime; advanced `plugin-render` specifically requires the source checkout containing the plugin templates and generates a separate private package.

## Material limits and deliberate choices

- **One canonical state root on one host.** Its locks coordinate only cooperating clients using that root. Do not register the same bot in another root or run another host's collector. Telegram cannot reveal every existing long-poll receiver through a read-only status call.
- **Host scoping is explicit.** Separate capability files and MCP processes are tested. A global MCP entry shared by several Codex chats gives those chats the same permission scope. Guided setup makes the scope an exact project directory; all its chats share one identity. Native scoped tools were tested live; plugin UI installation/enablement and automatic onboarding discovery were not demonstrated.
- **Listening is explicit.** On-demand use remains the default. The model-mediated relay needs a running chat; one bounded idle-target wakeup was verified. A manually started bounded direct dispatcher also passed an actual desktop/Telegram idle wake/reply/ack check without model forwarding. Automatic startup through the corrected 0.5.1 GUI gate remains pending a final restart check. Both reuse the collector without flushing sends; ordinary scoped polling can progress previously authorized queued sends. No system service, scheduler or MCP Events adapter is installed.
- **No exactly-once Telegram claim.** A lost response can follow acceptance. Unknown sends require reconciliation; operator-authorized retry may duplicate. Broker delivery can precede or outlive successful Telegram publication.
- **Finite queues and upstream retention.** The default pending limit is 1,000 per client. A full queue rolls back the whole fetched batch and holds the shared cursor; this can block other destinations until capacity is restored. Paused clients retain queued/new work until that limit. No configured update is silently dropped to advance the cursor. Offline Telegram updates may expire before intake; detected long gaps remain visible after reconnect.
- **Retention is explicit.** The latest 100 unassigned records contain metadata only, not replayable bodies. Configured inbox/outbox history stays until state retirement; acknowledgment does not erase content. There is no automatic purge/export/transfer tool. The scoped history reader exposes only the captured last 24 hours; it does not import preconnection messages or the old bridge archive. Suspended or revoked clients require owner-reviewed fresh pairing; old queues are not automatically reassigned.
- **Trusted same-user local processes.** Scoped tools do not defend against a hostile process with the same OS user's filesystem access. Protected files are the fallback; OS keyring integration and at-rest encryption are not implemented. Windows is unsupported by this flock implementation; macOS and Python 3.11/3.12 are untested.
- **Live evidence is bounded.** Native scoped tools and the selected shared-bot pause/resume and peer flow were checked live. General Telegram visibility/privacy combinations, external-bot interoperability, actual network-failure recovery and plugin UI loading remain unvalidated. Fake-transport results alone support none of those claims.

This Codex setup work does not migrate another receiver or inspect its credentials/messages. Receiver migration remains a separate owner-authorized procedure in [LIVE_VALIDATION.md](LIVE_VALIDATION.md); the live scoped-tool checks above are not evidence that an existing bridge was migrated.
