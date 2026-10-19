# Agents over Telegram: proposal

Date: 2026-10-19. Status: proposed MVP, authorized for local implementation.

## Purpose and core requirement

Make Telegram a convenient place for humans and their coding agents to coordinate. Each human configures **one bot once**. Their agents reuse that bot through a plugin and bind to dedicated groups or topics. Creating a bot per agent is explicitly outside the intended setup experience.

The bot belongs to the human; agents are logical identities managed by the bridge. Telegram still shows the shared bot as sender. Outgoing messages should clearly identify the logical agent, without presenting that label as a separate Telegram-authenticated identity.

Initial target: a local Codex plugin backed by reusable MCP tools and a setup/coordination skill. Keep the transport independent of Codex so other MCP clients can use it later.

## Intended experience

1. The human installs the plugin and registers an existing bot through a private setup command or UI. The token never needs to be pasted into the agent conversation or supplied as a tool argument.
2. Setup validates the bot and checks for an existing webhook/receiver. It explains conflicts instead of replacing an active integration.
3. The human adds that bot to a dedicated Telegram group, or chooses a forum topic in a group where it is already present.
4. From an agent chat, the user starts a connection. The bridge creates a short-lived, single-use pairing code. The human sends the addressed pairing command in the target conversation and confirms the detected destination locally.
5. The bridge records an agent binding: owner, bot account, Telegram chat/topic, logical agent ID, display label and permitted operations. The agent receives an opaque connection reference, not a credential.
6. Adding another agent repeats only the destination-binding step. The existing bot account and token are reused.
7. Status shows the connected destination, logical identity, last successful intake, pending messages and delivery problems. Pause/disconnect acts on that binding without breaking other agents.

Commands and tool names below are proposed interfaces, not claims about an existing product. Ordinary Telegram broadcast channels are not the default: MVP destinations are groups and forum topics, where discussion and replies work naturally. Add true channel support only after defining its distinct permissions and reply behavior.

## Verified platform facts and evidence limits

OpenAI documents plugin packages that combine a manifest, skills and MCP definitions. Validate the current desktop schema during implementation; authoring/distribution support varies by surface. [Package your plugin](https://developers.openai.com/plugins/build/plugins)

OpenAI currently documents MCP Events for Work web, desktop Work with Cloud selected, and dots. That does not establish immediate wakeups for this local Codex chat. MVP polling and later event adapters must be described separately. [MCP Events](https://developers.openai.com/plugins/build/mcp-events)

Telegram supports bot-to-bot group interactions through addressed commands or replies when the relevant communication mode is enabled. Broader visibility depends on privacy/admin settings. Telegram also calls for loop safeguards. Setup should verify delivery with the chosen destination rather than assume it. [Bot-to-bot communication](https://core.telegram.org/bots/features#bot-to-bot-communication)

Telegram uses mutually exclusive webhook and getUpdates intake. Advancing the getUpdates offset confirms older updates, and server retention is limited to 24 hours. Persist locally before advancing, and expose prolonged offline periods as a possible gap. [Getting updates](https://core.telegram.org/bots/api#getting-updates)

The existing private Remozio helper demonstrated group sends and retrieval of another bot's messages. It is a small single-group prototype, not proof of plugin installation, multi-agent isolation, automatic wakeups or exactly-once delivery. A direct bot DM attempt failed in that setup; groups remain the MVP path.

## Architecture

### One bot account, one intake owner, many agent bindings

Maintain one collector and durable Telegram cursor per bot account, shared by every local client. A new MCP session must never start an independent getUpdates consumer for the same bot. Local processes coordinate through a private lock and shared transactional storage. If a separate host already owns intake, require an explicitly configured relay or a different deployment; a local lock cannot protect against another machine.

Use one small service/core with an MCP adapter. Prefer Python 3.11+ and the official Python MCP SDK unless compatibility testing reveals a concrete reason otherwise. SQLite is a reasonable default for transactions, routing records, per-recipient acknowledgments, leases and outbox state. Pin dependencies and record supported platforms.

The first milestone may run bounded intake on demand while MCP clients are active. If a managed collector is necessary, its start/stop lifecycle must be explicit and documented. Installation must not silently install an operating-system daemon. Receiving messages into a queue and waking an idle agent are separate capabilities.

### Routing

Default binding key: bot account plus Telegram chat ID and optional message_thread_id. One dedicated destination maps to one logical agent. A group with several topics can therefore host several agents under the same bot.

If multiple agents share a topic, require explicit aliases or known reply routing. Ambiguous messages stay in an owner-visible unassigned queue; do not broadcast them to every agent and trigger multiple answers. Treat usernames/display labels as labels; use Telegram-provided numeric IDs for sender identity and locally registered IDs for agents.

Route interleaved updates for all configured destinations before advancing the bot-wide cursor. Bound and minimize retention of unconfigured-destination data; do not expose it to unrelated bindings. Persist each routed recipient delivery separately so one agent's acknowledgment cannot remove another's work.

Messages sent by agents sharing the same bot cannot depend on Telegram echoing that bot's own outbound messages as incoming updates. When same-bot agent-to-agent coordination is supported, the bridge must create the authorized internal delivery itself and correlate it with the Telegram publication. Mark origin/provenance explicitly, avoid duplicate fan-out, and surface uncertain publication separately. Never trust an agent name written in arbitrary message text as an authenticated route.

Cross-human coordination uses each human's existing bot. Address the receiving bot with its configured command and logical destination, or reply to a known message. Do not force the current peer's /codex command on every installation; store peer command conventions as configuration.

### Delivery semantics

- Incoming updates: durable, idempotent intake; leased work per binding; explicit acknowledgment after handling; recoverable lease expiration after an agent crashes.
- Outgoing messages: caller-supplied idempotency key scoped to binding and destination, plus an outbox record persisted before sending. Distinguish queued, sending, sent, rejected and unknown outcomes.
- A timeout after possible acceptance is unknown, not success and not an automatic resend. Provide an operator-visible recovery path. Do not promise exactly-once Telegram delivery.
- Rate limits apply per bot, destination and logical conversation. Preserve bounded automated exchanges, duplicate suppression and a human pause mechanism. An unrelated human message must not silently reset every conversation's loop limit.
- Edited messages, bot removal, group-to-supergroup migration, topic changes, queue limits and reconnects need explicit behavior. Unsupported cases should fail clearly and preserve queued work.

### Credentials and authorization

Keep credentials outside projects and model-visible arguments, using a system credential store where practical and a documented protected-file fallback. Verify ownership/permissions for that fallback. Diagnostic errors must exclude token-bearing URLs, raw credentials and unbounded response bodies. Use fake tokens in fixtures.

A pairing code proves possession of a pending connection, not unrestricted authority. Bind it to the requesting local owner and require local confirmation of the actual sender and destination. A group participant or peer bot cannot change bindings, recipients or project authority simply by sending an instruction.

Bind MCP clients to allowed connection references at the server boundary; do not rely on an arbitrary agent_id supplied by the model as authorization. MVP should state its trust boundary honestly: same-user trusted local processes are not isolated from a malicious process running as that OS user. Remote or multi-user access needs real client authentication and is a later deployment mode.

The plugin supplies communication. Repository instructions, approvals and readiness checks remain in the agent's task context. Incoming Telegram content is collaboration data, not an override of those instructions.

## Suggested tool surface

| Tool | Purpose |
| --- | --- |
| accounts_status | List safe metadata for the owner's registered bot accounts; no token output. |
| connection_begin | Create an expiring pairing request for an agent and existing bot. |
| connection_confirm | Bind a verified destination after local owner confirmation. |
| connections_list / connection_status | Show permitted bindings, health and backlog. |
| messages_poll | Intake as appropriate, then claim a bounded page for the authenticated binding. |
| messages_ack | Acknowledge only the caller's claimed deliveries. |
| message_send | Send or return prior outcome for a scoped idempotency key. |
| delivery_status | Inspect sent/rejected/unknown outcomes without blindly retrying. |
| connection_pause / connection_disconnect | Stop the binding's activity while preserving other agents. |

Keep token registration in the private setup flow rather than an MCP tool that accepts the token. Return structured, bounded results. Hide implementation mechanics from ordinary connection UX.

## Implementation milestones

1. **Reusable core and setup:** package the project; implement one bot account with two destination bindings, durable store, collector ownership, private credential registration and fake Telegram transport. Cover restart and cursor behavior before any live integration.
2. **MCP and plugin:** implement the small tool surface, setup/coordination skill and validated local plugin package. Document setup once, attach many agents, disconnect and uninstall. Prove two independent clients can share one bot without sharing acknowledgments.
3. **Isolated integration evidence:** run deterministic fault/concurrency tests and an MCP client smoke. Prepare an opt-in live procedure for a test bot/group: two agents, two topics/destinations, replies, restart and pause. Record precisely what was run.
4. **Optional wakeup/remote transport:** investigate supported event or scheduler integration after the local plugin works. Do not claim the plugin wakes local chats until demonstrated on the intended host. Hosted relay, cross-host ownership and public directory publication are separate deliverables.

## Acceptance criteria

- Register a fake bot once, attach two agents and two groups/topics, and demonstrate correct incoming and outgoing routing without additional bot credentials.
- Interleave and replay updates; crash before/after transaction commit; restart intake. Every accepted update is recoverable and one binding cannot acknowledge another's delivery.
- Run concurrent MCP clients; demonstrate one collector owner and independent queues, with bounded lock/lease recovery rather than deadlock.
- Exercise forged labels, wrong destinations, expired/reused pairing codes, unknown senders, stale connections and revoked bindings. Verify permission checks at the server boundary.
- Simulate rejection, rate limiting, transport timeout after server acceptance, and crash before saving send confirmation. Unknown sends remain visible and do not duplicate automatically.
- Verify same-bot logical delivery does not depend on getUpdates echo and cannot recursively resend itself. Apply bounded interaction limits to both same-bot and cross-bot traffic.
- Test queue limits, pagination, edited messages, disconnect/reconnect and offline gap reporting. Never silently discard another configured group's updates.
- Use distinctive fake secrets in every error path; assert they never appear in tool output, logs, packaged files or fixtures.
- Validate manifest/schema and MCP interoperability locally. Include reproducible install/start/test commands and a supported-host matrix.
- Keep the existing Remozio bridge untouched. Initial tests use fake transport and isolated state. A future live migration requires a deliberate single-receiver handoff with rollback.

## Non-goals for the first version

No bot per agent requirement, autonomous code execution from Telegram, automatic merge/deploy authority, public hosting, unrelated repository edits, unattended changes to BotFather/webhook settings, broad media/history ingestion, or claims of guaranteed delivery/instant wakeup. No OpenAI API key is required merely to transport messages between already-running agents.

