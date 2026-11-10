# Local operations

One human registers one bot once in one private state directory. Every logical agent reuses that account through its own protected client file and dedicated group or forum topic. All clients using that state share one transactional collector cursor; they have separate delivery leases, acknowledgments, outbound idempotency records and permissions.

The commands below use the absolute `aot` executable returned by the plugin's runtime bootstrap, or the runtime already configured for an existing project. Set `AOT_BIN` to that path; it need not be on `PATH`. No OpenAI API key is needed to transport messages. Runtime installation does not install a daemon, enable a plugin, or start collection. The normal plugin contains setup and coordination skills, with no global MCP declaration; guided setup creates the native MCP entry for the selected project.

## Codex setup

The package contains a Codex marketplace entry for its root plugin. Register the release repository and install the plugin with the Codex CLI:

```sh
codex plugin marketplace add fourlastor/agents-over-telegram
codex plugin add agents-over-telegram@agents-over-telegram
```

The public release repository is [fourlastor/agents-over-telegram](https://github.com/fourlastor/agents-over-telegram). Fresh GitHub marketplace registration, plugin installation and runtime bootstrap passed in a clean Codex home; the desktop install UI remains a separate check. The marketplace name and plugin name are both `agents-over-telegram`.

Ask **“Connect this agent to Telegram”** in the intended project. The setup skill first reuses that project's configured runtime, if present. For a new project it runs the bundled bootstrap with a compatible Python interpreter. You do not need to clone and maintain another checkout.

### Prepare the runtime

The bootstrap needs a local **Python 3.11+** interpreter. It uses uv when available; otherwise it creates a private environment with Python's `venv`/`ensurepip` and installs through that environment's pip. uv is optional and is never installed automatically. The fallback needs Python's virtual-environment and pip bootstrap support; distributions may provide these separately, for example `python3-venv` on Debian/Ubuntu or the matching versioned package for another interpreter. Use the official [Python downloads](https://www.python.org/downloads/) and [venv documentation](https://docs.python.org/3/library/venv.html) if these are missing. A manual invocation looks like this, using the actual installed plugin path:

```sh
python3 "/absolute/path/to/installed/plugin/scripts/bootstrap_runtime.py"
```

Use a different Python executable if `python3` is older than 3.11. The script copies the package's runtime source into a private stable runtime directory, creates a private virtual environment from the bundled dependency locks and installs the package non-editably. Its default root is `$XDG_DATA_HOME/agents-over-telegram/runtimes`, or `~/.local/share/agents-over-telegram/runtimes` when `XDG_DATA_HOME` is unset; `--runtime-root /absolute/private/path` selects another location. The package contents and Python interpreter identity select each runtime, and a validated matching runtime is reused without reinstalling its dependencies. The executable therefore stays available when the plugin cache changes.

On success, stdout is one JSON object with `status: "ready"`, `version`, `executable`, `runtime_dir`, `reused` and the selected `installer`. An error exits with status 2 and reports an actionable JSON message on stderr. Use the returned absolute `executable` path; do not assume a `.venv` inside the plugin cache. Bootstrap does not use global pip, read bot credentials, write project configuration, collect messages or start listening.

The default `--installer auto` selects uv if available and otherwise pip; supplying `--wheelhouse` selects pip in auto mode. `--installer uv` or `--installer pip` explicitly selects one for diagnosis or reproducible deployment; ordinary setup needs neither flag. `--installer uv` cannot be combined with `--wheelhouse`. The pip fallback installs both runtime and build dependencies from the shipped pinned, hashed requirements, using compatible wheels only. The existing uv path retains frozen runtime lock resolution; its isolated build dependencies are not claimed to have the new pip path's full transitive hash pinning.

For offline installation, add `--offline`. A valid existing runtime can be reused directly. A new uv installation requires its cached locked dependencies. A new pip installation additionally requires `--wheelhouse /absolute/path/to/wheels`, populated with compatible wheels for both `requirements/runtime.txt` and `requirements/build.txt`; merely having pip's cache is insufficient. Missing wheels or unsupported interpreter/platform combinations fail without fetching from the network. These offline controls are optional operator features, not first-time setup steps.

Developers regenerate the requirements after updating `uv.lock` with `python3 scripts/export_requirements.py`, using uv as a development tool. `python3 scripts/export_requirements.py --check` verifies the generated files. They are shipped in the plugin; installed bootstrap consumes them without uv, and users do not regenerate them during setup.

For an existing project, keep using the command already configured in its `[mcp_servers.telegram]` entry. A plugin upgrade does not automatically migrate that command or discard the old runtime. If its executable is unavailable, report the broken path and follow a deliberate repair rather than silently pointing the project at a different installation.

To move a Claude Code project to a newer runtime on purpose, update the plugin, run the new plugin's bootstrap, then run setup with the returned executable and `--prepare-only`. Setup records the runtime that runs it in `.aot-runtime.json`. It keeps the client, its pairing and its allowed senders. The plugin's MCP relay and hooks pick up the new runtime without a restart; see [agent hosts](HOSTS.md#claude-code). The `telegram-setup` skill has the exact steps.

### Pair the project

Use the returned executable for the terminal wizard:

```sh
AOT_BIN="/absolute/path/from/bootstrap/executable"
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
"$AOT_BIN" unsuspend
```

Interactive setup ends with a concise status and next step; `--prepare-only` returns JSON. Omit `--human` for JSON status. The older explicit `pause --client-file PATH --resume` form also remains supported. Existing explicit `--state-dir` and `--client-file` forms remain available for operator work; conflicts with an exact-directory profile are rejected rather than silently switching identities. Within a configured project, `flush` also uses only that project's client. Outside a configured project, the advanced unscoped `flush` command retains its bot-wide behavior. Do not read the client capability file to diagnose a profile.

Status reports the logical label, actual bot, destination, state, backlog, allowed senders, intake health and a suggested next action. It does not collect or flush. History is also read-only; see [captured context](#read-captured-context-from-the-last-24-hours). Pausing retains incoming work and stops new claims/sends for that binding. Resuming allows later authorized work; it does not start a timer, automatic agent replies or background monitoring. Resume does not clear a suspension. The owner clears it with `unsuspend` after fixing the cause; see [clear a suspended connection](#clear-a-suspended-connection).

The setup command and scoped native tools are distinct from installing the plugin through Codex's plugin UI. The manifest declares onboarding, but UI installation and setup-button invocation remain untested. See [compatibility](COMPATIBILITY.md) for evidence limits.

## Optional desktop listening

Ask **“Listen for Telegram”** with the target local chat and listening period. Keep ChatGPT open normally from the app list. The [desktop listener](DIRECT_DISPATCH.md) binds the existing client to an exact local thread UUID and matching project, then uses native queueing and the desktop wake URL when messages arrive. It requires no custom desktop launch, replacement backend or running relay model. Setup, status and resume do not start it automatically.

Use the [shared binding procedure](RELAY.md#select-the-target), then run:

```sh
"$AOT_BIN" --state-dir "$AOT_STATE" relay-desktop --duration 55 --collect
```

`--collect` opts into Telegram intake through the existing shared collector. Without it, the listener watches captured local work only. It does not flush queued sends, lease inbox messages or acknowledge them. The target uses its existing scoped Telegram tools for handling and authorized replies. The adapter supports Linux and macOS and checks that the installed `codex queue --help` exposes `--thread` and `--message`; the native binary defaults to `/usr/lib/chatgpt/resources/codex` on Linux and is discovered inside the installed ChatGPT/Codex app on macOS. macOS uses native process APIs and `/usr/bin/open -a` with the validated bundle; see [platform checks](DIRECT_DISPATCH.md#macos-process-and-url-checks). Version numbers are not an allowlist. The existing desktop process, local schema and target-routing checks still apply. Live validation used bundled Codex **0.163.0-alpha.10.2**; a successful capability check on another build is not a completed live round-trip test.

The listener checks that the desktop is running and validates the exact target and project before submitting. A fixed trusted notice enters the native queue as a user message; Telegram bodies and sender instructions never enter that notice. A known enqueue receipt permits retrying only the wake URL. Ambiguous enqueue results remain held until inspected. Native queueing, OS URL acceptance, model completion, inbox acknowledgment and Telegram publication are distinct outcomes.

An unresolved wake blocks every later wake for its client. After 15 minutes, `status` reports it as `unresolved_wake`, `status --human` prints a `Wake:` line, `relay-status` shows the same entry, and a running `relay-desktop` logs a `desktop_wake_stuck` warning at most once an hour per notification. Nothing is resolved automatically. Act on its `next_action`:

- `wait_for_desktop_wake`: the desktop queue already accepted the notice. Open the desktop app or resume the connection, and a running `relay-desktop` retries the wake URL by itself. If no listener is running, start one; nothing else retries the wake. Do not resolve it by hand, because that stops the retries.
- `review_failing_wake`: the same queued notice, but its wake URL failed 10 times while the desktop was open and the connection was active. Check that the target chat still exists and that the wake URL opens it. Opening that chat in the desktop app delivers the queued notice. Then record `accepted`, or `cancelled` to dismiss it. `failed` is refused, because the desktop queue already holds the notice.
- `review_uncertain_wake`: check the target chat, then record one outcome.

```sh
"$AOT_BIN" --state-dir "$AOT_STATE" relay-status --client-file "$AOT_CLIENT"
"$AOT_BIN" --state-dir "$AOT_STATE" relay-resolve --notification-id NOTIFICATION_ID --outcome accepted
```

Use `accepted` if the notice reached the chat, `failed` if it definitely did not, or `cancelled` to dismiss it. These are alternatives, not a sequence. See [long-held unresolved wakes](DIRECT_DISPATCH.md#a-long-held-unresolved-wake-blocks-later-wakes) for details.

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
"$AOT_BIN" --state-dir "$AOT_STATE" account-register --receiver-handoff-confirmed
"$AOT_BIN" --state-dir "$AOT_STATE" account-status
```

`account-register` prompts for the token with hidden local terminal input. It checks `getMe` and webhook metadata; it does not poll, send, or remove webhooks. Existing webhook configuration is refused. An existing registered credential is reused. The receiver-handoff flag is a deliberate owner assertion that another long-poll consumer is not running; Telegram cannot prove this by a read-only inspection. Stop an old receiver through its owner before confirming handoff.

Create the first client and start pairing:

```sh
"$AOT_BIN" --state-dir "$AOT_STATE" client-create --label Alpha --client-file "$AOT_STATE/clients/alpha.json"
"$AOT_BIN" --state-dir "$AOT_STATE" pair-begin --client-file "$AOT_STATE/clients/alpha.json"
```

The returned pairing command expires after ten minutes and is single-use. The human sends that exact addressed command in Alpha's intended group/topic. Then collect one bounded page and inspect the pending observation:

```sh
"$AOT_BIN" --state-dir "$AOT_STATE" collect
"$AOT_BIN" --state-dir "$AOT_STATE" pair-confirm --pairing-id PAIRING_ID
```

Review the observed numeric sender, chat and topic IDs locally. Confirm that observation, not a display label or a claim in a message:

```sh
"$AOT_BIN" --state-dir "$AOT_STATE" pair-confirm --pairing-id PAIRING_ID --confirm
"$AOT_BIN" --state-dir "$AOT_STATE" status --client-file "$AOT_STATE/clients/alpha.json"
```

For Beta, repeat `client-create`, `pair-begin`, collection and confirmation with `beta.json` and a different group/topic. Do not repeat bot creation or token registration. Only one logical agent can own a destination in this MVP. Group IDs and topic IDs are not interchangeable; no ordinary channel or private-bot-DM setup is promised.

### Additional senders and peers

Pairing authorizes the observed owner. Group membership and BotFather visibility settings do not authorize other senders in this plugin, unless the owner turns on [auto-allow](#auto-allow-in-a-trusted-group). Existing grants from a different bot or retired receiver do not transfer automatically. Use the operator's intended scope and verified numeric identities for local grants:

```sh
"$AOT_BIN" --state-dir "$AOT_STATE" allow-sender --client-file "$AOT_STATE/clients/alpha.json" --sender-id 123456
"$AOT_BIN" --state-dir "$AOT_STATE" grant-peer --source-client-file "$AOT_STATE/clients/alpha.json" --target-client-file "$AOT_STATE/clients/beta.json"
```

Use `allow-sender --bot` only for an intended external bot's verified numeric ID. A peer grant is directional; grant the reverse direction separately only when wanted. A message text such as “I am Beta” cannot create a grant. These grants permit transport routing; the agent still needs user authorization for the communication task.

To stop accepting a sender, run `allow-sender --sender-id 123456 --remove`. The owner cannot be removed. A removed sender stays out, also with auto-allow on, until it is allowed again with `allow-sender`.

#### Auto-allow in a trusted group

In a group where everyone is trusted, collecting each person's and each bot's numeric ID gets tedious. The owner can let AOT do it:

```sh
"$AOT_BIN" --state-dir "$AOT_STATE" delivery-settings --client-file "$AOT_STATE/clients/alpha.json" --auto-allow on
```

The command asks the owner to type `yes` at a terminal first, or, with no terminal attached, to [approve it in Telegram](#approve-a-change-in-telegram). With auto-allow on, every person and every bot who posts a message in the bound group or topic becomes an allowed sender the first time AOT sees them. From then on, they're handled exactly like a sender added with `allow-sender`: the filters, media, reactions, the bot exchange policy and the conversation budgets all apply as usual. Keep in mind that anyone who can post in the group can then give the agent work, including people who join later through an invite link.

- Only messages add senders. A reaction alone doesn't.
- Telegram's service accounts (linked channel posts, anonymous admins, the Telegram service account) are never added, and messages sent on behalf of a chat stay refused as before.
- An ID that is already allowed as a person isn't added again as a bot, or the other way round.
- An automatic grant never replaces an explicit one. Running `allow-sender` for an automatically added sender turns it into an explicit grant.
- `aot status` lists automatically added senders with `auto_added`, the time they were added, and `username`, the Telegram username seen then. `status --human` marks them `auto`.
- `--auto-allow off` stops adding new senders. Senders added so far stay allowed; remove them one by one with `allow-sender --remove`.

#### Approve a change in Telegram

Some changes need the owner's say-so: `delivery-settings --auto-allow on`, `--model-judge on` and `--keep-alive on`, and a `conversation-budget` grant. At a terminal, the command asks the owner to type `yes`. When no terminal is attached, which is how agents run commands, AOT asks the owner in Telegram instead:

- The command changes nothing yet. It returns `status: "owner_approval_pending"` with an `approval_id` and `expires_at`.
- The shared bot sends the owner a private message. It names the agent and the group, lists every value the command will set (one per line, including settings that need no approval), repeats the warning, and has **Approve** and **Decline** buttons. Free text such as a budget reason is shown on one line and shortened.
- **Approve** applies the whole command at once, including any other settings in it. **Decline** drops it. After either, the message shows the outcome and its buttons go away.
- Only a press from the owner's own Telegram account, in the private chat with the bot, counts. Presses by anyone else, from a forwarded copy, on a request that expired, or after the connection was paired again with a new owner are refused.
- A request expires after 10 minutes. Each connection can have at most 3 waiting at a time.
- By default the request goes to the owner's private chat. With `delivery-settings --approvals-in group`, it goes to the bound group or topic instead, as an *ephemeral* message: only the owner and the bot can see it, and nobody else in the group sees the request or its outcome. Only a press on that very message counts, from the owner it was sent to. Telegram doesn't guarantee that an ephemeral message arrives, especially when the owner is offline. If the group send fails, AOT sends the request to the private chat instead, and the command's result says `where: "private"`. `--approvals-in private` switches back.
- The owner must have sent `/start` to the bot in a private chat once, as for [owner notices](#notify-the-owner-privately). Otherwise the command fails with `owner_chat_not_started`.
- `aot status` lists the last few requests under `owner_approvals`, with their status: `pending`, `approved`, `declined`, `expired`, `failed` (the message could not be sent) or `apply_failed` (approved, but the change no longer applied, for example after a disconnect).

- The agent that asked is told the outcome. AOT delivers one `owner_approval` delivery to it, with `approval_id`, `action`, `params`, `outcome` (`approved`, `declined`, `apply_failed` or `expired`) and `error`. It wakes the agent like a message from the owner. A request that runs out unanswered is reported as `expired` at the next collection.

An agent can start a request but can't complete one. Changing the send limits for the whole bot (`exchange-limits`) still needs a terminal.

#### Settings panel in Telegram

The owner can change their agents' settings from a panel in their private chat with the shared bot. It's a Telegram HTML widget (Bot API 10.4). To get it, the owner sends **/settings** to the bot. An agent or the terminal can also send it:

```sh
"$AOT_BIN" --state-dir "$AOT_STATE" settings-panel --client-file "$AOT_STATE/clients/alpha.json"
```

- One panel covers every connection the owner has on that bot (up to 8), with a picker at the top. Each connection shows the agent, the group and every setting with its current value highlighted: the filter, context, media, reaction wake, notice batching (presets from off to 30 minutes), what skips batching, keep-alive, auto-allow, the model judge, owner notices and where approvals are asked.
- A tap changes the setting at once and the panel shows the stored value. A value set from the terminal that isn't one of the presets shows with no highlight, plus the seconds for batching. The panel asks the bot for fresh values each time it comes on screen.
- Turning on a setting with a warning (the model judge, auto-allow, keep-alive) first shows the same warning as the terminal. Turning a setting off needs no second tap.
- Only the owner's own tap counts, only in their private chat, only on the latest panel AOT sent them, and only for connections they own at that moment. A re-pair to a new owner or a disconnect takes a connection out of the panel. Sending /settings again, at most once every 10 seconds, retires the old panel.
- The owner must have sent `/start` to the bot once. Otherwise the panel can't be sent (`owner_chat_not_started`).
- Telegram currently shows widgets only in the private chat with the bot, and sends them as protected content, so they can't be forwarded and phones block screenshots while one is on screen. Apps without widget support show the message as unsupported.
- A tap must be answered within 15 seconds, so it needs a running collector: a relay or desktop listener, or a Claude Code session between turns. A tap while nothing collects shows a timeout in the panel and changes nothing; tap again later.
- AOT sets the bot's command menu in private chats to `/start` and `/settings`, once. Group menus aren't touched.
- A setting with a warning shows it right under that setting, with **Turn on** and **Cancel**.
- **Close** deletes the panel message and retires the panel. Send /settings for a new one.
- Bot-wide send limits and conversation budget grants aren't in the panel.

For an external bot already present in the same bound destination, the owner can configure its addressed command convention. This prefixes normal publications in that destination; it does not choose another Telegram chat or grant the external sender access:

```sh
"$AOT_BIN" --state-dir "$AOT_STATE" configure-peer --client-file "$AOT_STATE/clients/alpha.json" --bot-username PeerBot --command review
```

This produces an addressed `/review@PeerBot` prefix. Numeric sender allowance remains separate. Cross-human bot discovery, remote relay and end-to-end interoperability are not automated.

### Addressed-only delivery and group context

These settings are optional and off by default. Without them, every message from an allowed sender is delivered and messages from other senders are discarded. [Media](#attachments) is on by default for new connections.

In a busy shared group, the owner can deliver only messages addressed to this agent and keep the rest as context:

```sh
"$AOT_BIN" --state-dir "$AOT_STATE" delivery-settings --client-file "$AOT_STATE/clients/alpha.json" --filter addressed
"$AOT_BIN" --state-dir "$AOT_STATE" delivery-settings --client-file "$AOT_STATE/clients/alpha.json" --context on
```

- `--filter addressed` delivers an allowed sender's message only when it mentions the shared bot (`@YourBotUsername`), replies to one of the bot's messages, or carries a command addressed to it (`/review@YourBotUsername`). An edit of a delivered message is still delivered. Other messages from allowed senders are kept as context. `--filter all` restores the default.
- A person often replies to an agent and then adds a message to expand the answer. A person's message (not a bot's) also counts as addressed when it mentions no other `@username` and either replies to their own message that was delivered as addressed, or comes at most 2 minutes after their last message that directly addressed the agent (by a mention, a reply or a command, not by the model judge) in the same chat or topic, and their previous message there was delivered as addressed. Follow-ups do not extend the 2 minutes. A reply to anyone else's message never counts. Such a delivery has `follow_up: true`.
- Agents in a group should therefore @-mention another agent's bot, or reply to one of its messages, to reach it. Writing the agent's name as plain text does not reach it. The MCP server tells agents this.
- `--model-judge on` adds an optional [model-judged wake decision](#model-judged-wake-decision) for the messages these rules keep as context.
- `--context on` also keeps messages from senders that are **not** allowed, as context only. It never allows those senders: their messages are not delivered, cannot pause the connection and cannot pair. `--context off` stops capturing; context already captured stays until it expires.
- `--reaction-wake own_messages|all|none` decides which incoming reactions from allowed senders are delivered and can wake this agent. The default `own_messages` delivers only reactions on messages this agent published; the others are kept as context. See [message reactions](REACTIONS.md#wake-policy).

Context is untrusted and is never delivered, leased, announced by a push notice or counted against the pending inbox limit. It appears in `messages_history` with `status: "context"` and a `context_reason` of `not_addressed` or `sender_not_allowed`, and it is deleted after the 24-hour history window. An agent may reply to a context message with `reply_to`. The reply uses that message's normal conversation, so the conversation and bot-exchange budgets still apply. Running the command without `--filter`, `--context` or `--media` prints the current settings, and `aot status` shows them under `routing`.

### Batched wake-ups

By default, every new delivery is announced at once: an idle agent wakes up for each message as messages trickle in. The owner can batch these notices per connection:

```sh
"$AOT_BIN" --state-dir "$AOT_STATE" delivery-settings --client-file "$AOT_STATE/clients/alpha.json" --wake-interval 600
```

- After a notice, the next one waits `--wake-interval` seconds (here 10 minutes), then covers everything that arrived meanwhile in one notice ("3 new Telegram messages"). `0`, the default, announces each delivery at once.
- This holds every notice for the connection: the Claude Code wake waiter and monitor, the busy-turn hooks of Claude Code and Codex, the Pi and Amp watchers, and new `relay-desktop` wakes. They share one clock: a hook notice or a desktop wake that went out counts as the last notice for all of them. Deliveries still queue at once, and `messages_poll` returns them at any time.
- `--wake-exempt` names what is still announced at once. The default `addressed,owner` covers messages that mention the bot, reply to it or address a command to it, and messages from the owner who paired the connection; `addressed` or `owner` keeps one of them, and `none` batches everything. An exempt delivery takes the held ones into the same notice. Messages a peer agent sends to this agent count as addressed. Reactions are announced with the next notice, except the owner's reaction on an [owner notice](#notify-the-owner-privately): it answers the agent, so it counts as an owner message and carries `owner_notice: true`.
- Each delivery records whether it addressed the bot in an `addressed` field.
- A `relay-desktop` retry of a wake that already started is never held.

### Keep an idle Claude Code session listening

In Claude Code, an idle session hears Telegram through a background waiter that a turn end starts. The waiter stops after 23 hours. A session that stays idle for longer than that misses messages until someone sends it a prompt. Keep-alive closes that gap:

```sh
"$AOT_BIN" --state-dir "$AOT_STATE" delivery-settings --client-file "$AOT_STATE/clients/alpha.json" --keep-alive on
```

- When the waiter reaches its limit with no new message, it wakes the session once. The notice asks the model to end the turn at once. The turn end starts a new waiter for another 23 hours.
- The cost is one short model turn per idle day. That turn reads the whole conversation again, and after a day the prompt cache has expired, so it is not cached.
- It is off by default. With it off, the waiter stops quietly as before. Because it spends the owner's money, turning it on asks the owner to type `yes` at a terminal, or to [approve it in Telegram](#approve-a-change-in-telegram) when no terminal is attached.
- It applies only to Claude Code's waiter. Codex, Pi, Amp and `relay-desktop` don't use it.

## Launch one scoped MCP client

```sh
"$AOT_BIN" --state-dir "$AOT_STATE" mcp --client-file "$AOT_STATE/clients/alpha.json"
```

MCP uses stdin/stdout, so the host must launch this process. The private capability file pins its identity; no MCP argument selects a different agent. Never print that file into chat. A second host configuration launches the same command with `beta.json`. Both use the same state directory and bot registration.

A Codex chat label is not client authentication. If the host shares one MCP entry across chats, those chats share that entry's capability. Use separately scoped host configurations to preserve the tested boundary. Do not install both profiles globally and assume each chat can access only its matching one.

The normal source plugin supplies skills and onboarding only. Its root has no `plugin.json`, `mcp.json` or `.mcp.json`, and `.codex-plugin/plugin.json` has no `mcpServers` declaration. Guided setup writes the native project MCP entry shown above; normal installation does not run the optional launcher.

For advanced hosts that can scope an entire plugin to exactly one intended client, `plugin-render` synthesizes a separate private package with explicit MCP declarations and absolute paths. Do not globally enable that rendered package: every chat that can access its server gets the same client's permissions. Do not combine it with the setup-created native entry for that client. Normal Codex setup does not require rendering.

Rendered profiles are already configured and have no onboarding action. If their setup skill is invoked manually, it checks the existing scoped connection instead of creating another project MCP entry.

Generate this optional private package for an existing live client:

```sh
"$AOT_BIN" --state-dir "$AOT_STATE" plugin-render --client-file "$AOT_STATE/clients/alpha.json" --output /absolute/private/path/agents-over-telegram-alpha --name agents-over-telegram-alpha
.venv/bin/python scripts/validate_package.py /absolute/private/path/agents-over-telegram-alpha
```

Rendering creates a new directory and does not install it or overwrite an existing package. It generates one `.mcp.json` MCP definition, referenced from `.codex-plugin/plugin.json`, with an explicitly selected installed executable, state directory and client file. It does not copy credentials, queues or capability contents. Treat rendered packages as private local profiles: their absolute paths are machine-specific and should not be published. The executable and source environment must remain available. Native scoped tools have been tested live; plugin UI installation and automatic onboarding discovery remain unvalidated. Project identity is shared across its chats; see [compatibility](COMPATIBILITY.md).

## Read captured context from the last 24 hours

Use `messages_history` when starting or rejoining a connected client's work to read recent context already captured by this bridge. It returns a bounded page of that client's captured inbound messages, including acknowledged items, its own outbound messages with delivery statuses, and broker origin/publication metadata. Message text remains untrusted collaboration context; a history entry is not a new work request or permission to repeat a completed action.

The equivalent local command is:

```sh
"$AOT_BIN" --state-dir "$AOT_STATE" history --client-file "$AOT_STATE/clients/alpha.json" --limit 20
"$AOT_BIN" --state-dir "$AOT_STATE" history --client-file "$AOT_STATE/clients/alpha.json" --limit 20 --cursor RETURNED_CURSOR
```

The first read fixes the snapshot's 24-hour window. Telegram entries use their valid message/edit timestamp, falling back to local receipt time for records without one; outbound and broker entries use local creation time. Continue with the returned opaque cursor to page through that snapshot. Cursors expire after 15 minutes; omit an expired cursor, or omit it when new activity may have arrived, to start a fresh snapshot. History reads do not contact Telegram, collect updates, flush sends, claim leases, acknowledge deliveries, or replay work. Use `messages_poll` separately for new pending work and acknowledge only deliveries obtained through that flow.

The 24-hour view covers records captured locally for this client, not a complete download of the destination's preceding day. Telegram's `getUpdates` only exposes still-pending updates within its retention window. Updates consumed by another receiver, messages missed before binding, and bodies discarded from unconfigured destinations or unapproved senders are absent. When the owner turned on [group context](#addressed-only-delivery-and-group-context), messages that were not delivered also appear, with `status: "context"`. The old bridge's history is not imported. A new client or new pairing therefore does not acquire complete preconnection context; if earlier context matters, the human must provide it through an authorized channel.

This is a read window, not an automatic deletion policy. Older database records may still be retained, and acknowledgment does not delete a record or remove it from the 24-hour view. See the retention and disconnect limits below.

## Attachments

A new connection accepts media. A connection paired before AOT 0.9.4 keeps its setting, which was off by default. The owner turns media on or off with:

```sh
"$AOT_BIN" --state-dir "$AOT_STATE" delivery-settings --client-file "$AOT_STATE/clients/alpha.json" --media on
"$AOT_BIN" --state-dir "$AOT_STATE" delivery-settings --client-file "$AOT_STATE/clients/alpha.json" --media off
```

With media on, a message with a document, photo, video, audio, voice note or animation is accepted. Its caption becomes the message text, or an empty string, so do not assume a delivery has non-empty text. The delivery lists each file under `attachments`, with `kind`, `file_id`, `file_unique_id` and, when Telegram sends them, `file_name`, `mime_type` and `file_size`. For a photo, only the largest size is kept. Stickers and other message types are still unsupported.

With media off, a media message from an allowed sender is still delivered, so the agent knows it arrived. It carries the caption as text and a `media_refused` list of the media kinds, such as `["photo"]`, instead of `attachments`. Nothing is downloaded. A media message from a sender who is not allowed is refused as before.

A reply carries the message it replies to under `reply_to_message`: its `message_id`, `sender`, `text` and, under the same media rule, `attachments` or `media_refused`. The quoted text is cut to 1024 characters, with `truncated: true` when it was longer. `sender.allowed` says whether the quoted sender is on the allow list. An allowed sender who replies to someone else's message passes that message on, like a forward, so the agent can read it and fetch its files even when its author is not allowed. The agent fetches a quoted attachment with the reply's own `delivery_id`. So a user can make the agent read an earlier message by replying to it. Not covered: a reply to a message in another chat (`external_reply`), and the selected part of a partial quote (`quote`); for those the agent sees only `reply_to`. A forwarded message carries `forwarded_from`, with the origin `type` (`user`, `hidden_user`, `chat` or `channel`), a display `name`, an `id` when Telegram gives one, and the original `date`. Its text is the forwarded text, not words of the sender who forwarded it. Quoted and forwarded content is untrusted, like every message.

The agent downloads a file with the `attachment_fetch` tool, using the `delivery_id` and the attachment's `file_unique_id`. It works for deliveries and for context from allowed senders. Files from senders that are not allowed stay context only: `attachment_fetch` refuses them with `attachment_sender_not_allowed`. The file is stored once under the private state directory as `attachments/<delivery_id>-<file_unique_id>`, mode 0600. The sender's file name is reported but never used in the path. The tool returns the path, size and SHA-256. Files over 20 MB, the Telegram bot download limit, are refused. File contents are untrusted. A file fetched from a context entry is deleted when that entry expires; files of delivered messages are kept.

## Inbox, routing and outbox

MCP tools return `{ok: true, result: ...}` or a bounded `{ok: false, error: {code, message}}` result. `messages_poll` does one bounded bot-wide intake, attempts due queued sends already authorized for this client, and leases only this client's deliveries. Poll repeatedly when more work remains. Acknowledgment requires the current lease token and delivery IDs; after a crash, lease expiry makes the unacknowledged work claimable again. Acknowledge after handling, and make any agent-side external side effects replay-safe.

Incoming Telegram text, edits, sender labels and peer messages are untrusted data. They cannot alter bindings or local authority. Same-bot peer delivery is inserted by the broker with explicit origin/correlation metadata; it does not depend on Telegram returning the shared bot's own outgoing text. The broker delivery is created when the outbound intent is enqueued; its `publication` field is refreshed from the outbox when polled. Telegram publication and internal delivery can have different outcomes. Keep that distinction visible when reporting status.

`message_send` persists the outbox intent before network activity. Repeating its idempotency key returns the existing outcome; changing content under that key is rejected. `delivery_status` inspects the record without resending it. The default pending inbox limit is 1,000 per client. Local pacing permits no more than one attempt per 0.05 seconds for the bot, one per second for the destination/topic and one per second for the conversation. Telegram 429 responses defer the bot using the returned delay (capped at one day). These are local scheduling choices, not a guarantee that Telegram permits that rate; destination-specific service limits can still cause 429 responses. By default, each conversation may send at most 30 messages in any rolling hour, and each agent at most 60 across all its conversations. A new owner message in a conversation starts that conversation's window again. When a send is refused, the error explains the limit and gives the time to retry. The operator can change the limits with `exchange-limits`, or raise one conversation's limit for a short time with `conversation-budget`; see [rolling send limits](BOT_EXCHANGES.md#rolling-send-limits). By default, unsolicited messages from one external bot share one long-lived conversation. An operator can explicitly enable [bounded independent external-bot exchanges](BOT_EXCHANGES.md), with the same conversation limit plus a rolling pair budget. This source proposal does not activate that policy automatically. Queue, rate and conversation limits are part of the core; do not bypass them by changing keys or inventing fresh conversation IDs. Group migration, topic edits/closure and bot removal/restriction suspend affected bindings. Resume does not clear suspension. After the owner verifies the destination and permissions, disconnect and create a fresh client/pairing; this MVP does not automatically migrate destinations or transfer their retained inbox history.

A send can attach one local image with `image_path`: an absolute path to a regular PNG, JPEG, WebP or GIF file of at most 10 MB. The file is read and checked before anything is queued or stored: an unreadable path, a non-regular file, an oversized file or an unsupported type is rejected with a fixed error, and nothing is sent or written. PNG, JPEG and WebP are published with `sendPhoto`; a GIF, which Telegram will not take as a photo, is published with `sendDocument`. The labeled text becomes the caption, so with an image the caption, agent label header included, is limited to 1024 characters; longer text is rejected instead of cut. The idempotency key covers the image content hash: reusing a key with different text or a different image is rejected, exactly as for text. A peer send publishes the image in the peer's bound conversation, where people see it; the peer agent's broker delivery carries the caption text only, with no image reference it can fetch.

For a retry, the outbox stores a copy of the image bytes, not the original path. The bytes are written under the private state directory as `outbox-images/<outbox_id>.<ext>` (mode 0600) before the outbox row is committed, so the original file may change or disappear without affecting a queued record. Allowed recipients, pacing, conversation budgets, `delivery_status` and uncertain-send recovery apply to image sends unchanged. The copy is deleted when the record reaches `sent` or `rejected`; an `unknown` record keeps it until the operator resolves it, because `retry` may send it again. Repeating an idempotency key re-reads and re-checks `image_path`, so a retry whose source file is gone fails with `invalid_image`; once the intent is stored, `delivery_status` reports its outcome without needing the file.

Agents can also set or remove the bot's reaction on a known message in their own destination with `message_react`, and receive people's reactions as deliveries or context. Reactions carry their own rolling limit (30 per agent per hour), never count toward any message send limit, and require the bot to be an administrator in the chat for the receiving side. See [message reactions](REACTIONS.md).

Collection preserves the whole batch transaction: a full pending inbox rolls back that batch and holds the shared cursor. This prevents loss but can temporarily stall intake for other destinations. Resume and handle the affected inbox to free capacity before further collection. Acknowledgments release pending capacity but retain the records.

The owner can inspect bounded unassigned metadata:

```sh
"$AOT_BIN" --state-dir "$AOT_STATE" unassigned
```

Only the latest 100 update IDs, chat IDs, reason codes and timestamps are retained, with 20 shown per call. Message bodies from unconfigured destinations or unapproved senders are not stored in this queue. It is diagnostic evidence, not a replayable message archive; after fixing setup, ask the intended sender to resend if needed.

## Notify the owner privately

The owner often mutes the agent groups. An agent can use `owner_notify` to reach the owner directly when something needs them. The shared bot sends the notice to the owner's private chat with the bot. The owner is the person who paired the agent's group or topic.

Use it only for things that need the owner personally:

- a decision;
- a blocker;
- an approval;
- a direct question to the owner that has gone unanswered in the group.

Never use it for routine updates, progress reports or thanks. Those belong in the group, with `message_send`.

The notice starts with a label that names the agent and the group, for example `[Agent: Alpha | Release team]`. The agent can also pass `forward_message_id`. The bot then forwards that group message after the notice, so the owner sees the original. The message must belong to the agent's own group or topic: a message AOT captured there, or a message the shared bot published there. This is the same rule as for `message_react`.

AOT enforces the volume, not the agent. Each agent can send at most 3 notices in any rolling hour and 10 in any rolling day. A notice that Telegram refused does not count. A notice with an `unknown` outcome counts, because it may have reached the owner.

`owner_notify` is off for every connection until the owner turns it on. It is the first tool that writes outside the agent's own group or topic, so the owner decides for each connection. During setup, the agent asks the owner after pairing and runs the command only on a yes. The owner can also turn it on or off at any time:

```sh
"$AOT_BIN" --state-dir "$AOT_STATE" delivery-settings --client-file "$AOT_STATE/clients/alpha.json" --owner-notify on
"$AOT_BIN" --state-dir "$AOT_STATE" delivery-settings --client-file "$AOT_STATE/clients/alpha.json" --owner-notify off
```

`connection_status` shows the setting as `routing.owner_notify`.

The owner must also do one thing once: open a private chat with the bot and send `/start`. Telegram does not let a bot start a private chat by itself. The bot answers the `/start` with one short message. It says how many connected agents the owner has on this bot, and how many of them can send notices. Only the owner of at least one connected agent on the bot gets this answer, at most once a minute. Anyone else gets no answer. A paused or suspended connection refuses `owner_notify`, as for `message_send`.

Only Telegram replies reach an agent. To answer, the owner uses Reply on a notice (swipe left on the message, or long-press and choose Reply). A plain message typed into the private chat is not delivered to any agent. Several agents can share one bot, so a plain message has no single agent to go to. A Telegram reply names one message, and AOT knows which agent it belongs to. Each notice therefore ends with "Reply to this message to answer the agent."

- When the owner replies to a notice, to its forward, or to one of their own messages that was already routed, AOT delivers the reply to that agent. An edit of a routed message is delivered too, as `kind: "edited"`; an edit of any other private message is not. It is a normal message delivery from the owner, with the private chat as `chat_id`, `owner_notice: true` and `addressed: true`. So it wakes the agent at once, also while wake batching holds other notices. Text and media follow the same rules as group messages, including the connection's media setting.
- The agent can answer in the group, or with another `owner_notify` that passes `reply_to`: the `message_id` of its own notice, of its forward, or of an owner reply it received. The answer then appears as a Telegram reply in the private chat. Any other `reply_to` fails with `unknown_owner_reply`. An answer to a message the owner sent the agent continues a conversation the owner started. It does not count toward the notice limits and has its own cap: 30 per agent per rolling hour, refused with `owner_reply_limit`. A `reply_to` on the agent's own notice or forward is a normal notice and counts as usual.
- To ask a quick question, the agent passes `choices`: 2 to 4 short answer labels, for example `["Yes", "No"]`. Each label must be 1 to 32 printable characters, with no leading or trailing spaces, and labels must differ. The notice then has one button per label, and its last line says the owner can tap an answer or reply.
  - The owner's press reaches the agent once, as a delivery with `kind: "owner_choice"`, `choice` (the label), `choice_index`, `notify_id`, `notice_message_id` and `owner_notice: true`. It joins the notice's conversation and wakes the agent like an owner message.
  - The message then shows the answer, and its buttons go away. A second press only gets a short note.
  - Only the owner's own press, on that very notice, in a connection that still has that owner, counts. The buttons expire after 24 hours. A press after that asks the owner to reply instead.
  - The buttons don't replace replies. A text reply to the notice reaches the agent as usual, and it doesn't use up the buttons.
  - Once the owner has pressed a button, a `reply_to` on that notice answers the owner and counts toward the owner reply cap, not the notice limits.
- The owner can also react to a notice, or to its forward, to acknowledge it. The reaction reaches the agent that sent the notice as a normal reaction delivery on its own message, with the private chat as `chat_id`. It wakes the agent under the same `reaction_wake` rules as other reactions on its own messages.

Only the owner's own replies and reactions are routed this way. AOT keeps every other private chat update as unassigned metadata with the reason `private_chat`. This includes `/start`, plain messages that are not a Telegram reply, replies and reactions on other messages, anything from other people, and the owner blocking or unblocking the bot. AOT stores no text and no private chat id for these, and it delivers them to no agent. `aot unassigned` shows these entries; they are expected, not a fault.

`owner_notify` uses the same idempotency rules as `message_send`. The same key with the same request returns the same outcome. The same key with a different request is refused. `delivery_status` shows the outcome. An `unknown` outcome follows [uncertain-send recovery](#uncertain-sends) with the `owner_…` id. A `retry` repeats only the step that has no recorded message: the notice text or the forward.

| Error code | Meaning |
| --- | --- |
| `owner_notify_disabled` | The owner has not turned on `owner_notify` for this connection. Nothing was sent, and it does not count toward the limit. The agent asks the owner; only the owner runs `delivery-settings --owner-notify on`. |
| `unknown_owner_reply` | `reply_to` is not a notice, forward or routed owner reply of this agent in the owner's private chat. Nothing was sent. |
| `owner_reply_limit` | The agent sent 30 answers to the owner's own messages in the last hour. Nothing was sent. The error gives `retry_at`. |
| `owner_notify_limit` | The agent reached the hourly or daily limit. Nothing was sent. The error gives `retry_at`, the time when the next notice is allowed. |
| `owner_chat_not_started` | Telegram refused the private chat (403): the owner never sent `/start` to the bot, or blocked it. Nothing was sent, and it does not count toward the limit. Ask the owner to send `/start` to the bot, then use a new idempotency key. |
| `unknown_forward_target` | `forward_message_id` is not a message in the agent's own group or topic. Nothing was sent. |
| `forward_rejected` | Not an error of the call. The status is `sent` because the owner got the notice text, but Telegram refused the forward, for example because the message was deleted. |

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
"$AOT_BIN" --state-dir "$AOT_STATE" resolve --outbox-id OUTBOX_ID --resolution accepted --message-id MESSAGE_ID
"$AOT_BIN" --state-dir "$AOT_STATE" resolve --outbox-id OUTBOX_ID --resolution cancel
"$AOT_BIN" --state-dir "$AOT_STATE" resolve --outbox-id OUTBOX_ID --resolution retry
```

These are alternatives, not a sequence. `accepted` records a verified publication. `cancel` stops further attempts while preserving the record. `retry` is an explicit decision to permit another attempt when the operator accepts duplication risk; it changes the original record to `queued` and does not send immediately. Continue the original intent/key. A later `messages_poll`, repeated `message_send` with that same intent/key, or explicit scoped flush can attempt the now-due record. The same command and resolutions apply to uncertain reactions (`--outbox-id react_…`) and owner notifications (`--outbox-id owner_…`); `accepted` needs no `--message-id` there:

```sh
"$AOT_BIN" --state-dir "$AOT_STATE" flush --client-file "$AOT_STATE/clients/alpha.json"
```

The owner may omit `--client-file` to flush due sends across all clients. Flush attempts up to 20 due queued records; it never retries records still marked `unknown`. `collect` only takes in updates and does not flush the outbox. There is no timer: without a later poll/send/flush call, a delayed queued send remains queued. Neither a timeout nor an absent recent update proves Telegram rejected a publication. There is no exactly-once Telegram guarantee.

### Model-judged wake decision

The rules above cannot catch every message meant for an agent, for example a person who answers the agent's question after a pause, without replying or mentioning the bot. With `--filter addressed`, the owner can let a decision model judge those messages. Any endpoint that speaks TypeSafe's System One decision API works: [Jev](https://docs.typesafe.ai/introduction/quickstart), Cloudflare's [Clef and Clef-flash](https://developers.cloudflare.com/workers-ai/models/clef-flash/) on Workers AI, or a Clef you host yourself, which costs nothing per call.

```sh
"$AOT_BIN" --state-dir "$AOT_STATE" judge-config set --endpoint https://api.typesafe.ai/v1/systemone --model jev-latest --key
"$AOT_BIN" --state-dir "$AOT_STATE" delivery-settings --client-file "$AOT_STATE/clients/alpha.json" --model-judge on
```

- `judge-config set` stores the endpoint and the model name it expects in `judge.json` in the private state directory, with the same protections as the bot token. For example:
  - Jev: `--endpoint https://api.typesafe.ai/v1/systemone --model jev-latest`
  - Cloudflare: `--endpoint https://api.cloudflare.com/client/v4/accounts/ACCOUNT_ID/ai/run/@cf/cloudflare/clef-flash --model clef-flash`
  - A self-hosted Clef on this computer, at its own address and with no key.
- `--key` asks for an API key in a hidden terminal prompt; leave it out for a model that needs none. The key never appears in tool arguments, tool results or status.
- The endpoint must use https. Plain http is allowed only to this computer (`localhost` or a loopback address).
- `--threshold` sets how sure the model must be: AOT delivers when the model's probability for yes is at least this value. The default is `0.5`; raise it toward `0.9` for fewer, surer wakes.
- `judge-config status` shows the endpoint, the model, the threshold and whether a key is stored. `judge-config clear` deletes the configuration. One configuration serves every connection on this computer that turns the judge on.
- `--model-judge on` is off by default and set per connection. It asks you to type `yes` at a terminal, or to [approve it in Telegram](#approve-a-change-in-telegram) when no terminal is attached, because it sends group text to the configured model. The model sees the agent's label and bot username, up to 8 earlier messages from the same chat or topic (this agent's own posts included, each cut to 600 characters), and the message itself. `--model-judge off` stops it.
- Only a person's new message with text that the addressed filter kept as context is judged. Bot messages, edits, messages from senders who are not allowed, and messages that already count as addressed are never sent.
- When the model says yes, the message becomes a normal delivery with `addressed: true` and `addressed_by: "model"`, so wake batching announces it at once. Otherwise it stays context. A timeout (8 seconds), an error, a redirect, a missing configuration or an unclear answer also keeps it as context. AOT never retries a judgment.
- Only the long-running collectors judge: the Claude Code monitor and wake waiter, `watch --collect` and the relay listeners. Hooks and tool calls never wait on the model. The Claude Code monitor and wake waiter announce what a collection brought before they judge, so a message that addresses the agent never waits on the model either, and a message judged yes is announced right after. A message waits for the next long-running collection, and is dropped as context after 5 minutes.
- Each collection judges at most 3 messages and starts no call that could end more than 20 seconds after the first. Each connection gets at most 60 judgments in any rolling hour and 300 in any rolling day; messages over a limit stay context.
- Message text is untrusted for the model as for the agent. A message that talks the model into a yes only wakes the agent, and its text is still collaboration data.

## Credentials and stored data

This MVP uses a protected-file fallback, not a system keyring. The state directory and client-file parent directories are owned by the current OS user and restricted to mode `0700`; credentials and client files are mode `0600`. Reads reject unsafe owners/modes, symlinks, hard links and malformed or oversized JSON. Diagnostic messages use fixed codes and do not include token-bearing request URLs or raw response bodies.

The bot token lives in `credentials.json` under the private state root. The optional [model judge](#model-judged-wake-decision) configuration, with its key, lives in `judge.json` next to it. Each client capability lives in its own private JSON file. The database contains routing records, message text, queues and outbox history, so protect it and its backups as private data too. Do not place any of these files in a repository, share them as plugin resources, or inspect their contents through a model. At-rest encryption and OS-keyring integration are not implemented. A malicious process running as the same OS user is outside this isolation boundary.

## Pause, disconnect and uninstall

```sh
"$AOT_BIN" --state-dir "$AOT_STATE" pause --client-file "$AOT_STATE/clients/alpha.json"
"$AOT_BIN" --state-dir "$AOT_STATE" pause --client-file "$AOT_STATE/clients/alpha.json" --resume
"$AOT_BIN" --state-dir "$AOT_STATE" disconnect --client-file "$AOT_STATE/clients/alpha.json"
```

Pause retains incoming work, blocks new claims/sends, and prevents queued publications for that connection while other clients continue. Authorized intake still queues its messages up to the bound. The paired human owner can also send the exact addressed `/pause@YourBotUsername` command in that destination; it pauses only after the collector observes it. This transport pause has no authority to execute arbitrary Telegram commands. Resume only when requested by the owner. Disconnect revokes the selected client, marks queued outbound work from/to it `rejected`, preserves inbox/history records, and does not revoke the shared bot token. A send already in flight can finish after a pause/disconnect request; inspect its recorded result. Retained history is not automatically transferred to a newly paired client. The scoped `history` command reads the current active client's recent captured context; it does not provide a revoked-client archive export or replay path. Reconnecting requires a fresh client/pairing flow; do not reuse a revoked capability. Review pending and uncertain outgoing work before disconnecting.

### Clear a suspended connection

AOT suspends a connection when Telegram reports a change to its destination. A suspended connection keeps collecting authorized messages into its inbox, but the agent cannot read them or send until the owner clears the suspension. `status` shows the reason in `suspended`. The agent cannot clear a suspension: `unsuspend` is a local owner command and is not an MCP tool.

| Reason | Cause | What the owner does |
| --- | --- | --- |
| `bot_removed_or_restricted` | The bot left, was removed, or lost the right to post. | Add the bot back or restore its rights, then run `unsuspend`. |
| `topic_changed` | The bound forum topic was closed or edited. | Reopen the topic if needed, check it, then run `unsuspend --topic-open`. |
| `group_migrated:<new chat id>` | The group became a supergroup, and AOT could not move the connection by itself. See [group upgrades](#group-upgrades-to-a-supergroup). | Run `unsuspend`. It moves the connection to the new chat id. |

```sh
"$AOT_BIN" --state-dir "$AOT_STATE" unsuspend --client-file "$AOT_STATE/clients/alpha.json"
"$AOT_BIN" --state-dir "$AOT_STATE" unsuspend --client-file "$AOT_STATE/clients/alpha.json" --topic-open
```

Before it clears anything, `unsuspend` calls Telegram `getChatMember` for the bot in the bound chat. For `group_migrated`, it checks the new chat instead. It accepts `member`, `administrator`, or `restricted` with both `is_member` and `can_send_messages` true. Any other answer, or a Telegram error, leaves the suspension in place and prints an error code such as `bot_not_in_chat` or `bot_cannot_post`. The Bot API has no read-only call that shows whether one forum topic is open, so `--topic-open` records that the owner checked the topic. It does not post anything to the chat.

`unsuspend` does not change queued messages. It prints JSON with the previous reason, the new state, `pending_messages` and `queued_outgoing`. The held inbox messages become available to the agent's next poll, and queued outgoing messages are sent on the next flush. An owner pause stays in place; run `resume` separately. If the collector records any suspension event while the check runs, even one with the same reason, the command fails with `suspension_changed` and keeps the suspension. A new reason never replaces one that needs more owner work: a migration stays a migration, and a closed topic stays `topic_changed` even if the bot is also removed. Clearing `topic_changed` still checks the bot membership.

### Group upgrades to a supergroup

Telegram gives a group a new chat id when it becomes a supergroup. Telegram does this by itself when the owner changes some group settings. Telegram announces the change twice: in the old group (`migrate_to_chat_id`) and in the new supergroup (`migrate_from_chat_id`). When the collector sees either notice, AOT moves every connection of the old group to the new chat id. The client, its allowed senders, its held inbox messages and its queued outgoing messages stay the same. No owner action is needed.

AOT can also learn about the upgrade from a send. If it sends to the old group after the upgrade, Telegram refuses the send and names the new chat id (`migrate_to_chat_id`). Nothing was published, so AOT moves the connections as above and queues the message again, with the error `group_migrated`, for the next flush in the new chat. A reaction refused this way is rejected with `group_migrated` instead, because the message it targets stays in the old group, which Telegram has closed.

AOT suspends a connection with `group_migrated:<new chat id>` instead of moving it in two cases:

- Another connection already uses the new chat id. `unsuspend` then fails with `destination_already_bound` until that connection is disconnected.
- An AOT version older than this behavior observed the upgrade. Run `unsuspend`; it moves the connection to the new chat id.

The supergroup numbers its messages separately, and the old group's messages stay in the closed old group. A queued reply therefore moves as a plain message, without its reply link. A queued reaction is rejected with `group_migrated`, because the message it targets cannot be reached anymore. Without these rules, both would land on an unrelated message in the new chat that happens to have the same id.

Only queued outgoing messages move to the new chat id. A send already marked `unknown` stays on the old chat. After a migration, check the old chat in Telegram and settle it with `resolve --resolution accepted` or `resolve --resolution cancel`. Do not use `--resolution retry`: the retry would still target the old chat id. Do not run `disconnect` to fix a migration. `disconnect` revokes the client, so the agent then needs a new client and a new pairing.

Disabling or uninstalling the normal skills plugin leaves the setup-created native project MCP entry configured. To remove that connection from Codex, remove only the owned `[mcp_servers.telegram]` entry from that exact project's `.codex/config.toml`, preserve unrelated settings, and reload/reopen the project chat to stop using the old server configuration. Remove the project's `.aot-profile.json` when retiring its local CLI selection as well. For an advanced rendered plugin, remove/disable that scoped host entry and stop its process. These steps do not revoke the client; use `disconnect` first when revocation is intended. This package installs no service to uninstall. Retain the shared state while any other agents depend on it. Removing plugin files does not delete local state or revoke a bot token. To retire the whole bridge, stop all its processes, reconcile unknown sends, retain any required protected backup, then explicitly remove private state and client files. Token revocation is a separate deliberate owner action that affects all users of that bot, including integrations outside this package.

Acknowledged messages and outbox records remain until an operator retires state; this MVP has no automatic content-pruning job. Do not promise retention deletion from a message acknowledgment. If a queue reaches its bound or a collector reports a possible offline gap, inspect status before continuing. Telegram's retention window is finite; a local durable queue cannot recover updates that expired before intake.
