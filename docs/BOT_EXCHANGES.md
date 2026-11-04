# Proposed bounded external bot exchanges

This opt-in policy separates new external bot requests. Deployment and activation require a deliberate local operator decision. A source update alone does not enable it.

All automated sends are bounded by rolling rate limits, not by lifetime counters. Each conversation may send at most 30 messages in any rolling hour. Each agent may send at most 60 messages in any rolling hour across all its conversations. When an older send leaves a window, sending can continue. A long agent exchange therefore slows down when it is too busy, but it does not have to wait for a human to post. The operator can change all limits; see [rolling send limits](#rolling-send-limits).

The per-agent ceiling is what bounds the total. Without it, an agent that rotates through old conversations, or a bot that replies to old agent messages, would get a fresh 30 per hour in every conversation it can reach. The number of reachable conversations grows with every owner message.

## Contract

| Situation | Behavior |
| --- | --- |
| No enabled policy for this client and external bot | Preserve the legacy persistent `bot:CLIENT:SENDER` chain. Its rolling conversation limit applies. |
| Enabled policy; a new, admitted, unsolicited bot message | Assign a server-derived `bot-root:CLIENT:SENDER:MESSAGE_ID` exchange. Message text cannot select the ID. |
| Bot replies to a known sent publication | Inherit that publication's conversation and its rolling window. The per-agent ceiling still bounds the agent's replies across all such conversations. |
| Any sender edits an already captured message | Reuse its original conversation, even after policy disable or unknown-send recovery. |
| Unseen edit or reply to an unknown publication | Use the legacy chain; do not mint a new root. |
| Any conversation | Allow at most 30 reserved sends in a rolling hour (configurable). ACK, restart, policy toggles, and human messages elsewhere do not reset the window. Only time frees space. |
| Any agent (client), across all its conversations | Allow at most 60 reserved sends in a rolling hour (configurable). This holds whether or not a bot exchange policy is enabled. |
| Participating external bot pair | Allow at most 32 reserved sends in a rolling hour across its exchanges (configurable). Attach a pair to a human-started conversation when a real bot reply joins it. |
| Outbound reply with an explicit different conversation | Reject it with `reply_conversation_mismatch`. Without an explicit conversation, use the actual reply lineage. |

The core cannot prove that two unsolicited messages concern different semantic tasks. The rolling pair budget bounds bots that repeatedly post new roots instead of preserving reply lineage. It is a second limit, not evidence that every new root is independent work. Correct peers should reply to the actual message and keep the supplied conversation ID.

Reservations count queued, sent, rejected and unknown publications. No automatic refund follows a rejection or an uncertain send. Reusing a valid idempotency key does not consume a second reservation. Every new reservation and all budget checks occur in the existing transaction before network activity. Shared-bot broker sends use the same rolling conversation limit; if they continue an exchange with an external pair, that pair's rolling budget also applies.

Reaching the conversation limit returns `conversation_limit`. Reaching the per-agent ceiling returns `client_limit`. Reaching the pair limit returns `bot_pair_limit`. When several pairs are full, the error reports the latest time at which all of them have room. All three failures occur before an outbox intent is created, so nothing is sent or queued. There is no automatic retry, relay through another client, or invented conversation ID. The error tells the agent what happened and when to try again:

| Field | Meaning |
| --- | --- |
| `message` | Plain explanation: which limit was reached, the current usage, that nothing was sent, how long to wait, and that the agent must not switch conversations to avoid the limit. It tells the agent to stop and tell the human if the work cannot wait. It does not name the commands that change limits. |
| `limit`, `window_seconds` | The limit that applied, for example 30 sends per 3600 seconds. This includes any active operator grant. |
| `sent_in_window` | How many sends the window already holds. |
| `retry_after_seconds`, `retry_at`, `retry_at_utc` | The earliest time one more send fits, as a delay, a Unix timestamp, and an ISO 8601 UTC time. |

The agent should wait until `retry_at` and then retry the same send in the same conversation. A rejected send creates no outbox record, so the same idempotency key is valid for the retry. The timestamp does not prove that an old request is still appropriate to answer.

## Rolling send limits

The operator sets the limits once for the whole state directory. Every agent process that uses this bot reads them from the shared state, so they always agree. No MCP tool can change them.

| Limit | Default | Allowed range |
| --- | --- | --- |
| Sends per conversation per window | 30 | 1–1000 |
| Conversation window | 3600 seconds | 60–86400 seconds |
| Sends per agent across all conversations per window | 60 | 1–1000 |
| Agent window | 3600 seconds | 60–86400 seconds |
| Sends per external bot pair per window | 32 | 1–1000 |
| Pair window | 3600 seconds | 60–86400 seconds |

```sh
# Inspect the current limits.
"$AOT_BIN" --state-dir "$AOT_STATE" exchange-limits

# Change one or more limits. Options you omit keep their current value.
"$AOT_BIN" --state-dir "$AOT_STATE" exchange-limits \
  --conversation-send-limit 60 --conversation-window-seconds 3600 \
  --reason "Long agent coordination runs"

# Return to the built-in defaults.
"$AOT_BIN" --state-dir "$AOT_STATE" exchange-limits --reset --reason "Back to defaults"
```

A change needs a reason and takes effect on the next send. Running processes do not need a restart.

A change also needs a human at a terminal. The command prints the change and continues only after someone types `yes`. Without a terminal it fails with `operator_confirmation_required`. Agents usually run commands without a terminal, so this stops an agent from lifting its own limit by accident. It is not a security boundary: agents run as the same Unix user and could drive a terminal on purpose. Inspecting the limits never asks.

The limits slow a loop between agents; they do not stop it. With the defaults, one looping agent can still send 60 messages an hour, and each one can wake another agent. Lower the limits if that cost is too high.

## Local operator control

An operator can raise the rolling limit of one existing conversation for a short time, after an explicit human request:

```sh
"$AOT_BIN" --state-dir "$AOT_STATE" conversation-budget \
  --client-file "$AOT_CLIENT" --conversation-id EXISTING_CONVERSATION \
  --additional-sends 16 --duration-seconds 50400 \
  --reason "Human authorized a quota-break handoff"
```

Omit the last three options to inspect the allowance. While a grant is active, the conversation may send up to `--additional-sends` more messages in each rolling window. A grant adds at most 32 sends and lasts at most 24 hours.
Only a client that already takes part in the conversation can create a grant. The grant then raises the limit for every sender in that conversation, because all their sends count toward the same window. It preserves counters, reply lineage, and idempotency records. The per-agent ceiling still applies on top of a grant. Granting needs the same typed confirmation at a terminal as `exchange-limits`. Grants made by older versions were lifetime allowances; they do not apply to rolling windows and are ignored.
The grant is recorded in an append-only table. It does not change the rolling pair budget, queue limits, sender permissions, or pause state.
No MCP tool can grant an allowance. Existing processes must load the updated core to use it; older cores keep the default cap.
Status and grant commands do not recover interrupted sends. Expired grants do not reopen a conversation.

Only an already allowed external bot's verified numeric sender ID can receive this policy. The command does not grant a sender, change the group, or install a listener. No MCP tool can enable it.

Use the selected project's existing runtime, state, and protected client file. These are illustrative commands; do not execute enablement during source review:

```sh
"$AOT_BIN" --state-dir "$AOT_STATE" bot-exchange-policy \
  --client-file "$AOT_CLIENT" --sender-id VERIFIED_EXTERNAL_BOT_ID

# Only after the operator reviews and authorizes the new bounds:
"$AOT_BIN" --state-dir "$AOT_STATE" bot-exchange-policy \
  --client-file "$AOT_CLIENT" --sender-id VERIFIED_EXTERNAL_BOT_ID --enable
```

Omitting `--enable` and `--disable` inspects the current policy. Inspection and policy changes do not recover interrupted sends. `--disable` prevents future independent root creation. It creates no memberships for a pair that has never been enabled; memberships and rolling reservations already created stay enforced. Re-enabling does not reset them. Human/non-bot senders and unknown bot IDs are refused. Existing destination, sender, pause, suspension, rate, queue and deduplication checks remain in force.

## Additive migration

1. Prepare and validate this source in an isolated runtime using fake transport. Review the proposed bounds before deployment.
2. Coordinate a maintenance window for the selected account. Preserve a protected backup and account for pending or uncertain sends through the existing owner. Keep one receiver; do not inspect or copy credentials into a report.
3. Move all cooperating local MCP/collector processes to the reviewed runtime. A plugin cache update alone does not change an existing project runtime. Do not mix old and new cores after policy enablement.
4. Initialization adds `bot_exchange_policies`, `bot_exchange_members` and indexes. Existing conversations, send counts, inbox conversation IDs, ACKs, outbox records and idempotency keys are not rewritten. All policies default to disabled.
5. Inspect the intended allowed bot and its scope. After explicit authorization, enable only that pair. Legacy outbox reservations and captured historical conversations with that verified bot then participate in its rolling budget.
6. Validate with a genuinely new, authorized bot request. Do not replay or reroute a blocked old reply. Verify actual handling, one publication, ACK, and persistent counters after restart.

The new root policy applies to messages admitted after enablement. Already captured work keeps its original conversation. It does not automatically retry, reclassify, or resend any previously blocked status update.

For rollback, disable future root creation and review outstanding exchanges first. Preserve the new state. An older binary does not enforce the new pair budget on retained new-root conversations; do not downgrade an active opted-in account and claim the same guarantees. Restoring a pre-migration backup can also lose newly collected updates. Use the existing deliberate receiver handoff procedure rather than copying cursors or deleting tables.

## Offline verification

`tests/test_bot_exchanges.py` covers legacy schema migration without counter changes, a fresh independent exchange after legacy exhaustion, reply lineage sharing its conversation window, edits across policy disable, unknown replies and edits, a storm of fresh roots, rolling boundary behavior, restart and toggle persistence, uncertain/rejected sends, idempotency, concurrent last-slot allocation, sender/destination isolation, human-started lineage, reply budget borrowing, clock rollback, explicit CLI controls, disable without prior enablement, and human edit lineage after unknown-send recovery.

These checks use temporary state and `FakeTransport`. They do not establish live activation, cross-version operation, or compatibility with a peer that deliberately drops reply lineage. Keep live activation separate from this proposal.
