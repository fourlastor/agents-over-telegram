# Local operations

One human registers one bot once in one private state directory. Every logical agent reuses that account through its own protected client file and dedicated group or forum topic. All clients using that state share one transactional collector cursor; they have separate delivery leases, acknowledgments, outbound idempotency records and permissions.

The commands below use the `aot` executable installed from this checkout. It may not be on `PATH`; use its quoted absolute path when working in another project. No OpenAI API key is needed to transport messages. Runtime installation does not install a daemon, enable a plugin, or start collection. The normal plugin contains setup and coordination skills, with no global MCP declaration; guided setup creates the native MCP entry for the selected project.

## Codex setup

First install the Python runtime from a stable source checkout (Python 3.11+):

```sh
uv sync --frozen --python /usr/bin/python3.13
```

Use an available Python 3.11+ interpreter path on another host. An installed plugin copy in Codex's cache still needs this separate installed `aot` executable; a cache directory is not an installed Python environment. Keep the runtime directory available because the project configuration refers to its absolute executable path. There is no automatic runtime bootstrap.

In a Codex chat with this plugin available, ask **“Connect this agent to Telegram.”** The packaged `telegram-setup` onboarding skill locates that executable, helps select the project and reuses the existing bot. The local terminal wizard performs private token entry and destination confirmation:

```sh
AOT_BIN="/home/daniele/Documents/agents-over-telegram/.venv/bin/aot"
"$AOT_BIN" setup --label Alpha --project-dir "/absolute/path/to/alpha-project"
```

Replace the project path with the intended directory. If the shared bot is already registered elsewhere, add `--state-dir "/actual/shared/state"` before `setup`; do not create a second state root for the same bot. `--client-file "/private/existing-client.json"` attaches an existing authorized client. The wizard reuses existing setup when possible. For the first registration only, the interactive wizard explains receiver ownership and asks the human to type `yes` after establishing that no other receiver uses the selected bot. The `--receiver-handoff-confirmed` flag supplies that same assertion explicitly. An existing registered account is reused without another token prompt or account-validation network call. It never needs a token in chat, a command-line argument or a model-visible file read.

For a new binding, send the exact pairing command shown by the wizard to the intended Telegram group/topic, return to the terminal, and review the observed numeric sender, chat and topic before answering its confirmation. Do not send the actual conversation messages until pairing is confirmed: prebinding bodies are not retained. Use an addressed command such as `/test@YourBotUsername hello` for an initial check when Telegram privacy mode may filter ordinary text.

The wizard prepares two local files in that project: `.aot-profile.json` references the private state/client paths without copying their secrets, and `.codex/config.toml` configures one native `[mcp_servers.telegram]` entry while preserving unrelated settings. This project entry is the intended runtime connection. The normal plugin adds no second MCP server and needs no `AOT_EXECUTABLE`, `AOT_STATE_DIR` or `AOT_CLIENT_FILE` environment settings. Setup refuses conflicting configuration. These are local machine paths; do not publish the generated profile as a reusable configuration.

Only a profile in the **exact current project directory** is selected. There is no parent-directory search. All Codex chats in that project use the same Telegram identity and permission scope; use separate project directories for separate logical agents. A worktree or subdirectory is a different directory and needs deliberate setup. Open/add the prepared project in Codex, make the host's normal trust decision yourself, and reload/reopen its chat so Codex loads the project configuration. Verify the newly loaded native `telegram` tools with `connection_status`, checking the logical label, bot and destination. A local CLI status result alone does not establish host tool loading. The wizard does not set global trust or control the Codex UI.

For an already registered account, authorized setup can run the same command with `--prepare-only` to prepare files noninteractively without collecting updates. It prepares/reuses the client, pending pairing and project configuration, then reports the next step; it does not collect, send, flush or confirm pairing. Complete a pending pairing by running the same setup command without `--prepare-only` in the human's terminal. An already connected client skips pairing; a preparation rerun reuses its identity/configuration. For an unregistered live account, even `--prepare-only` still requires hidden token entry in a local terminal and the explicit receiver-ownership assertion. Completing pairing still requires the human's local destination review. An existing receiver needs the separate [handoff procedure](LIVE_VALIDATION.md); do not assert `--receiver-handoff-confirmed` until its owner has established that the selected bot has no competing receiver.

## Everyday controls

In Codex, use “Telegram status,” “Pause Telegram,” “Resume Telegram,” or “Show recent Telegram context.” These prompts select the scoped workflow; they are not invented slash commands. A direct pause/resume request authorizes that control and needs no second permission question.

Preferred local equivalents from the exact configured project directory are:

```sh
"$AOT_BIN" status --human
"$AOT_BIN" history
"$AOT_BIN" pause
"$AOT_BIN" resume
```

Interactive setup ends with a concise status and next step; `--prepare-only` returns JSON. Omit `--human` for JSON status. The older explicit `pause --client-file PATH --resume` form also remains supported. Existing explicit `--state-dir` and `--client-file` forms remain available for operator work; conflicts with an exact-directory profile are rejected rather than silently switching identities. Within a configured project, `flush` also uses only that project's client. Outside a configured project, the advanced unscoped `flush` command retains its bot-wide behavior. Do not read the client capability file to diagnose a profile.

Status reports the logical label, actual bot, destination, state, backlog, allowed senders, intake health and a suggested next action. It does not collect or flush. History is also read-only; see [captured context](#read-captured-context-from-the-last-24-hours). Pausing retains incoming work and stops new claims/sends for that binding. Resuming allows later authorized work; it does not start a timer, automatic agent replies or background monitoring. A suspended connection still needs owner review rather than a forced resume.

The setup command and scoped native tools are distinct from installing the plugin through Codex's plugin UI. The manifest declares onboarding, but UI installation and setup-button invocation remain untested. See [compatibility](COMPATIBILITY.md) for evidence limits.

## Optional desktop listening

Ask **“Listen for Telegram”** with the target local chat and listening period. Keep ChatGPT open normally from the app list. The [desktop listener](DIRECT_DISPATCH.md) binds the existing client to an exact local thread UUID and matching project, then uses native queueing and the desktop wake URL when messages arrive. It requires no custom desktop launch, replacement backend or running relay model. Setup, status and resume do not start it automatically.

Use the [shared binding procedure](RELAY.md#select-the-target), then run:

```sh
"$AOT_BIN" --state-dir "$AOT_STATE" relay-desktop --duration 55 --collect
```

`--collect` opts into Telegram intake through the existing shared collector. Without it, the listener watches captured local work only. It does not flush queued sends, lease inbox messages or acknowledge them. The target uses its existing scoped Telegram tools for handling and authorized replies. The current adapter is limited to Linux with bundled Codex **0.163.0-alpha.10.2**; the native binary defaults to `/usr/lib/chatgpt/resources/codex`.

The listener checks that the desktop is running and validates the exact target and project before submitting. A fixed trusted notice enters the native queue as a user message; Telegram bodies and sender instructions never enter that notice. A known enqueue receipt permits retrying only the wake URL. Ambiguous enqueue results remain held until inspected. Native queueing, OS URL acceptance, model completion, inbox acknowledgment and Telegram publication are distinct outcomes.

Stop listening when the requested period ends or the user asks. Setup installs no daemon, scheduler or autostart entry. An app-check/OS-opener race can reopen a just-closed app normally, and stopping cannot retract a notice already queued. See [desktop lifecycle and recovery](DIRECT_DISPATCH.md#notice-and-recovery-semantics) for details. An accepted notice that races a pause is not repeated after resume; inspect pending status and explicitly poll.

The normal-launch `relay-desktop` Telegram check passed on 2026-10-19: incoming marker `normal-idle-1` automatically woke the existing local Alpha chat, which polled once, sent one reply, acknowledged after recorded publication and returned to zero pending messages. Native tools and the ledger confirm the round trip; the user also confirmed the reply is visible in Telegram. Earlier `relay-watch` model-mediated listening and `relay-dispatch` shared-backend/bridge routes remain [experimental alternatives](DIRECT_DISPATCH.md#earlier-experimental-adapters), not default setup instructions.

## Offline first

From the source checkout:

```sh
python3 -m venv .venv
.venv/bin/python -m pip install -e '.[dev]'
.venv/bin/python -m pytest -q
.venv/bin/python scripts/validate_package.py
.venv/bin/aot demo
```

The demo creates temporary isolated fake state. `--fake` uses fake transport and never reads a bot token. Fake and live mode cannot be mixed in the same initialized state directory. Use the test report for the precise dependency lock and runtime that were validated.

## Advanced manual setup: register once, attach many

If Alpha already exists, reuse its exact registered state directory and skip `account-register` and its handoff flag. Start at `client-create` for Beta below. This adds no new receiver or token. Unrelated bots and bridges do not need consolidation or modification.

Use these lower-level commands when guided setup is unsuitable. Select the intended bot and establish its sole receiver before registration; see [live validation and migration](LIVE_VALIDATION.md). Set a task-specific shell variable for the private state location:

```sh
AOT_STATE="$HOME/.local/share/agents-over-telegram"
.venv/bin/aot --state-dir "$AOT_STATE" account-register --receiver-handoff-confirmed
.venv/bin/aot --state-dir "$AOT_STATE" account-status
```

`account-register` prompts for the token with hidden local terminal input. It checks `getMe` and webhook metadata; it does not poll, send, or remove webhooks. Existing webhook configuration is refused. An existing registered credential is reused. The receiver-handoff flag is a deliberate owner assertion that another long-poll consumer is not running; Telegram cannot prove this by a read-only inspection. Stop an old receiver through its owner before confirming handoff.

Create the first client and start pairing:

```sh
.venv/bin/aot --state-dir "$AOT_STATE" client-create --label Alpha --client-file "$AOT_STATE/clients/alpha.json"
.venv/bin/aot --state-dir "$AOT_STATE" pair-begin --client-file "$AOT_STATE/clients/alpha.json"
```

The returned pairing command expires after ten minutes and is single-use. The human sends that exact addressed command in Alpha's intended group/topic. Then collect one bounded page and inspect the pending observation:

```sh
.venv/bin/aot --state-dir "$AOT_STATE" collect
.venv/bin/aot --state-dir "$AOT_STATE" pair-confirm --pairing-id PAIRING_ID
```

Review the observed numeric sender, chat and topic IDs locally. Confirm that observation, not a display label or a claim in a message:

```sh
.venv/bin/aot --state-dir "$AOT_STATE" pair-confirm --pairing-id PAIRING_ID --confirm
.venv/bin/aot --state-dir "$AOT_STATE" status --client-file "$AOT_STATE/clients/alpha.json"
```

For Beta, repeat `client-create`, `pair-begin`, collection and confirmation with `beta.json` and a different group/topic. Do not repeat bot creation or token registration. Only one logical agent can own a destination in this MVP. Group IDs and topic IDs are not interchangeable; no ordinary channel or private-bot-DM setup is promised.

Additional senders and same-bot peers require local owner grants:

```sh
.venv/bin/aot --state-dir "$AOT_STATE" allow-sender --client-file "$AOT_STATE/clients/alpha.json" --sender-id 123456
.venv/bin/aot --state-dir "$AOT_STATE" grant-peer --source-client-file "$AOT_STATE/clients/alpha.json" --target-client-file "$AOT_STATE/clients/beta.json"
```

Use `allow-sender --bot` only for an intended external bot's verified numeric ID. A peer grant is directional; grant the reverse direction separately only when wanted. A message text such as “I am Beta” cannot create a grant. These grants permit transport routing; the agent still needs user authorization for the communication task.

For an external bot already present in the same bound destination, the owner can configure its addressed command convention. This prefixes normal publications in that destination; it does not choose another Telegram chat or grant the external sender access:

```sh
.venv/bin/aot --state-dir "$AOT_STATE" configure-peer --client-file "$AOT_STATE/clients/alpha.json" --bot-username PeerBot --command review
```

This produces an addressed `/review@PeerBot` prefix. Numeric sender allowance remains separate. Cross-human bot discovery, remote relay and end-to-end interoperability are not automated.

## Launch one scoped MCP client

```sh
.venv/bin/aot --state-dir "$AOT_STATE" mcp --client-file "$AOT_STATE/clients/alpha.json"
```

MCP uses stdin/stdout, so the host must launch this process. The private capability file pins its identity; no MCP argument selects a different agent. Never print that file into chat. A second host configuration launches the same command with `beta.json`. Both use the same state directory and bot registration.

A Codex chat label is not client authentication. If the host shares one MCP entry across chats, those chats share that entry's capability. Use separately scoped host configurations to preserve the tested boundary. Do not install both profiles globally and assume each chat can access only its matching one.

The normal source plugin supplies skills and onboarding only. Its root has no `mcp.json` or `.mcp.json`, and the compatibility manifest has no `mcpServers` declaration. Guided setup writes the native project MCP entry shown above; normal installation does not run the optional launcher.

For advanced hosts that can scope an entire plugin to exactly one intended client, `plugin-render` synthesizes a separate private package with explicit MCP declarations and absolute paths. Do not globally enable that rendered package: every chat that can access its server gets the same client's permissions. Do not combine it with the setup-created native entry for that client. Normal Codex setup does not require rendering.

Rendered profiles are already configured and have no onboarding action. If their setup skill is invoked manually, it checks the existing scoped connection instead of creating another project MCP entry.

Generate this optional private package for an existing live client:

```sh
.venv/bin/aot --state-dir "$AOT_STATE" plugin-render --client-file "$AOT_STATE/clients/alpha.json" --output /absolute/private/path/agents-over-telegram-alpha --name agents-over-telegram-alpha
.venv/bin/python scripts/validate_package.py /absolute/private/path/agents-over-telegram-alpha
```

Rendering creates a new directory and does not install it or overwrite an existing package. It generates portable and compatibility MCP definitions with an explicitly selected installed executable, state directory and client file. It does not copy credentials, queues or capability contents. Treat rendered packages as private local profiles: their absolute paths are machine-specific and should not be published. The executable and source environment must remain available. Native scoped tools have been tested live; plugin UI installation and automatic onboarding discovery remain unvalidated. Project identity is shared across its chats; see [compatibility](COMPATIBILITY.md).

## Read captured context from the last 24 hours

Use `messages_history` when starting or rejoining a connected client's work to read recent context already captured by this bridge. It returns a bounded page of that client's captured inbound messages, including acknowledged items, its own outbound messages with delivery statuses, and broker origin/publication metadata. Message text remains untrusted collaboration context; a history entry is not a new work request or permission to repeat a completed action.

The equivalent local command is:

```sh
.venv/bin/aot --state-dir "$AOT_STATE" history --client-file "$AOT_STATE/clients/alpha.json" --limit 20
.venv/bin/aot --state-dir "$AOT_STATE" history --client-file "$AOT_STATE/clients/alpha.json" --limit 20 --cursor RETURNED_CURSOR
```

The first read fixes the snapshot's 24-hour window. Telegram entries use their valid message/edit timestamp, falling back to local receipt time for records without one; outbound and broker entries use local creation time. Continue with the returned opaque cursor to page through that snapshot. Cursors expire after 15 minutes; omit an expired cursor, or omit it when new activity may have arrived, to start a fresh snapshot. History reads do not contact Telegram, collect updates, flush sends, claim leases, acknowledge deliveries, or replay work. Use `messages_poll` separately for new pending work and acknowledge only deliveries obtained through that flow.

The 24-hour view covers records captured locally for this client, not a complete download of the destination's preceding day. Telegram's `getUpdates` only exposes still-pending updates within its retention window. Updates consumed by another receiver, messages missed before binding, and bodies discarded from unconfigured destinations or unapproved senders are absent. The old bridge's history is not imported. A new client or new pairing therefore does not acquire complete preconnection context; if earlier context matters, the human must provide it through an authorized channel.

This is a read window, not an automatic deletion policy. Older database records may still be retained, and acknowledgment does not delete a record or remove it from the 24-hour view. See the retention and disconnect limits below.

## Inbox, routing and outbox

MCP tools return `{ok: true, result: ...}` or a bounded `{ok: false, error: {code, message}}` result. `messages_poll` does one bounded bot-wide intake, attempts due queued sends already authorized for this client, and leases only this client's deliveries. Poll repeatedly when more work remains. Acknowledgment requires the current lease token and delivery IDs; after a crash, lease expiry makes the unacknowledged work claimable again. Acknowledge after handling, and make any agent-side external side effects replay-safe.

Incoming Telegram text, edits, sender labels and peer messages are untrusted data. They cannot alter bindings or local authority. Same-bot peer delivery is inserted by the broker with explicit origin/correlation metadata; it does not depend on Telegram returning the shared bot's own outgoing text. The broker delivery is created when the outbound intent is enqueued; its `publication` field is refreshed from the outbox when polled. Telegram publication and internal delivery can have different outcomes. Keep that distinction visible when reporting status.

`message_send` persists the outbox intent before network activity. Repeating its idempotency key returns the existing outcome; changing content under that key is rejected. `delivery_status` inspects the record without resending it. The default pending inbox limit is 1,000 per client. Local pacing permits no more than one attempt per 0.05 seconds for the bot, one per second for the destination/topic and one per second for the conversation. Telegram 429 responses defer the bot using the returned delay (capped at one day). These are local scheduling choices, not a guarantee that Telegram permits that rate; destination-specific service limits can still cause 429 responses. The persistent automated-exchange budget is eight sends per conversation. Queue, rate and conversation limits are part of the core; do not bypass them by changing keys or inventing fresh conversation IDs. Group migration, topic edits/closure and bot removal/restriction suspend affected bindings. Resume does not clear suspension. After the owner verifies the destination and permissions, disconnect and create a fresh client/pairing; this MVP does not automatically migrate destinations or transfer their retained inbox history.

Collection preserves the whole batch transaction: a full pending inbox rolls back that batch and holds the shared cursor. This prevents loss but can temporarily stall intake for other destinations. Resume and handle the affected inbox to free capacity before further collection. Acknowledgments release pending capacity but retain the records.

The owner can inspect bounded unassigned metadata:

```sh
.venv/bin/aot --state-dir "$AOT_STATE" unassigned
```

Only the latest 100 update IDs, chat IDs, reason codes and timestamps are retained, with 20 shown per call. Message bodies from unconfigured destinations or unapproved senders are not stored in this queue. It is diagnostic evidence, not a replayable message archive; after fixing setup, ask the intended sender to resend if needed.

## Uncertain sends

| Outcome | Meaning and action |
| --- | --- |
| `queued` | Intent is durable, but has not been confirmed as sent. Inspect the reason and retry/backoff state. |
| `sending` | A process owns the attempt. A crash can leave an uncertain outcome; restart recovery must not blindly resend. |
| `sent` | A Telegram acceptance was saved or the operator reconciled an observed acceptance. This is not proof a human read it. |
| `rejected` | A definite rejection or explicit cancellation is recorded. Inspect status before creating a new intent. |
| `unknown` | Telegram may have accepted the send. Do not automatically retry or choose a new idempotency key. |

For `unknown`, the operator checks the intended Telegram destination and correlates the publication. Then choose exactly one local resolution:

```sh
.venv/bin/aot --state-dir "$AOT_STATE" resolve --outbox-id OUTBOX_ID --resolution accepted --message-id MESSAGE_ID
.venv/bin/aot --state-dir "$AOT_STATE" resolve --outbox-id OUTBOX_ID --resolution cancel
.venv/bin/aot --state-dir "$AOT_STATE" resolve --outbox-id OUTBOX_ID --resolution retry
```

These are alternatives, not a sequence. `accepted` records a verified publication. `cancel` stops further attempts while preserving the record. `retry` is an explicit decision to permit another attempt when the operator accepts duplication risk; it changes the original record to `queued` and does not send immediately. Continue the original intent/key. A later `messages_poll`, repeated `message_send` with that same intent/key, or explicit scoped flush can attempt the now-due record:

```sh
.venv/bin/aot --state-dir "$AOT_STATE" flush --client-file "$AOT_STATE/clients/alpha.json"
```

The owner may omit `--client-file` to flush due sends across all clients. Flush attempts up to 20 due queued records; it never retries records still marked `unknown`. `collect` only takes in updates and does not flush the outbox. There is no timer: without a later poll/send/flush call, a delayed queued send remains queued. Neither a timeout nor an absent recent update proves Telegram rejected a publication. There is no exactly-once Telegram guarantee.

## Credentials and stored data

This MVP uses a protected-file fallback, not a system keyring. The state directory and client-file parent directories are owned by the current OS user and restricted to mode `0700`; credentials and client files are mode `0600`. Reads reject unsafe owners/modes, symlinks, hard links and malformed or oversized JSON. Diagnostic messages use fixed codes and do not include token-bearing request URLs or raw response bodies.

The bot token lives in `credentials.json` under the private state root. Each client capability lives in its own private JSON file. The database contains routing records, message text, queues and outbox history, so protect it and its backups as private data too. Do not place any of these files in a repository, share them as plugin resources, or inspect their contents through a model. At-rest encryption and OS-keyring integration are not implemented. A malicious process running as the same OS user is outside this isolation boundary.

## Pause, disconnect and uninstall

```sh
.venv/bin/aot --state-dir "$AOT_STATE" pause --client-file "$AOT_STATE/clients/alpha.json"
.venv/bin/aot --state-dir "$AOT_STATE" pause --client-file "$AOT_STATE/clients/alpha.json" --resume
.venv/bin/aot --state-dir "$AOT_STATE" disconnect --client-file "$AOT_STATE/clients/alpha.json"
```

Pause retains incoming work, blocks new claims/sends, and prevents queued publications for that connection while other clients continue. Authorized intake still queues its messages up to the bound. The paired human owner can also send the exact addressed `/pause@YourBotUsername` command in that destination; it pauses only after the collector observes it. This transport pause has no authority to execute arbitrary Telegram commands. Resume only when requested by the owner. Disconnect revokes the selected client, marks queued outbound work from/to it `rejected`, preserves inbox/history records, and does not revoke the shared bot token. A send already in flight can finish after a pause/disconnect request; inspect its recorded result. Retained history is not automatically transferred to a newly paired client. The scoped `history` command reads the current active client's recent captured context; it does not provide a revoked-client archive export or replay path. Reconnecting requires a fresh client/pairing flow; do not reuse a revoked capability. Review pending and uncertain outgoing work before disconnecting.

Disabling or uninstalling the normal skills plugin leaves the setup-created native project MCP entry configured. To remove that connection from Codex, remove only the owned `[mcp_servers.telegram]` entry from that exact project's `.codex/config.toml`, preserve unrelated settings, and reload/reopen the project chat to stop using the old server configuration. Remove the project's `.aot-profile.json` when retiring its local CLI selection as well. For an advanced rendered plugin, remove/disable that scoped host entry and stop its process. These steps do not revoke the client; use `disconnect` first when revocation is intended. This package installs no service to uninstall. Retain the shared state while any other agents depend on it. Removing plugin files does not delete local state or revoke a bot token. To retire the whole bridge, stop all its processes, reconcile unknown sends, retain any required protected backup, then explicitly remove private state and client files. Token revocation is a separate deliberate owner action that affects all users of that bot, including integrations outside this package.

Acknowledged messages and outbox records remain until an operator retires state; this MVP has no automatic content-pruning job. Do not promise retention deletion from a message acknowledgment. If a queue reaches its bound or a collector reports a possible offline gap, inspect status before continuing. Telegram's retention window is finite; a local durable queue cannot recover updates that expired before intake.
