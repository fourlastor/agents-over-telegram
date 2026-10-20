# Proposed bounded external bot exchanges

This opt-in policy separates new external bot requests without deleting the old lifetime counters. Deployment and activation require a deliberate local operator decision. A source update alone does not enable it.

## Contract

| Situation | Behavior |
| --- | --- |
| No enabled policy for this client and external bot | Preserve the legacy persistent `bot:CLIENT:SENDER` chain and its eight-send cap. |
| Enabled policy; a new, admitted, unsolicited bot message | Assign a server-derived `bot-root:CLIENT:SENDER:MESSAGE_ID` exchange. Message text cannot select the ID. |
| Bot replies to a known sent publication | Inherit that publication's conversation, including an exhausted legacy chain. |
| Bot edits an already captured message | Reuse its original conversation, even after policy disable. |
| Unseen edit or reply to an unknown publication | Use the legacy chain; do not mint a new root. |
| Any exchange | Keep the existing permanent eight-send chain cap. ACK, time, restart, and policy toggles do not reset it. |
| Participating external bot pair | Allow at most 32 reserved sends in a rolling hour across its exchanges. Attach a pair to a human-started conversation when a real bot reply joins it. |
| Outbound reply with an explicit different conversation | Reject it with `reply_conversation_mismatch`. Without an explicit conversation, use the actual reply lineage. |

The core cannot prove that two unsolicited messages concern different semantic tasks. The rolling pair budget bounds bots that repeatedly post new roots instead of preserving reply lineage. It is a second limit, not evidence that every new root is independent work. Correct peers should reply to the actual message and keep the supplied conversation ID.

Reservations count queued, sent, rejected and unknown publications. No automatic refund follows a rejection or an uncertain send. Reusing a valid idempotency key does not consume a second reservation. Every new reservation and both budget checks occur in the existing transaction before network activity. Shared-bot broker sends retain the eight-send conversation cap; if they continue an exchange with an external pair, that pair's rolling budget also applies.

Reaching the chain cap returns `conversation_limit`. Reaching the rolling pair cap returns `bot_pair_limit`. Both failures occur before an outbox intent is created. There is no automatic retry, reset, relay through another client, or invented conversation ID. The local policy status reports `retry_at` when the rolling budget is full. That timestamp does not reopen an exhausted eight-send chain or prove that an old request is still appropriate to answer.

## Local operator control

Only an already allowed external bot's verified numeric sender ID can receive this policy. The command does not grant a sender, change the group, or install a listener. No MCP tool can enable it.

Use the selected project's existing runtime, state, and protected client file. These are illustrative commands; do not execute enablement during source review:

```sh
"$AOT_BIN" --state-dir "$AOT_STATE" bot-exchange-policy \
  --client-file "$AOT_CLIENT" --sender-id VERIFIED_EXTERNAL_BOT_ID

# Only after the operator reviews and authorizes the new bounds:
"$AOT_BIN" --state-dir "$AOT_STATE" bot-exchange-policy \
  --client-file "$AOT_CLIENT" --sender-id VERIFIED_EXTERNAL_BOT_ID --enable
```

Omitting `--enable` and `--disable` inspects the current policy. `--disable` prevents future independent root creation; existing exchange memberships and rolling reservations remain enforced. Re-enabling does not reset them. Human/non-bot senders and unknown bot IDs are refused. Existing destination, sender, pause, suspension, rate, queue and deduplication checks remain in force.

## Additive migration

1. Prepare and validate this source in an isolated runtime using fake transport. Review the proposed bounds before deployment.
2. Coordinate a maintenance window for the selected account. Preserve a protected backup and account for pending or uncertain sends through the existing owner. Keep one receiver; do not inspect or copy credentials into a report.
3. Move all cooperating local MCP/collector processes to the reviewed runtime. A plugin cache update alone does not change an existing project runtime. Do not mix old and new cores after policy enablement.
4. Initialization adds `bot_exchange_policies`, `bot_exchange_members` and indexes. Existing conversations, send counts, inbox conversation IDs, ACKs, outbox records and idempotency keys are not rewritten. All policies default to disabled.
5. Inspect the intended allowed bot and its scope. After explicit authorization, enable only that pair. Legacy outbox reservations and captured historical conversations with that verified bot then participate in its rolling budget. The exhausted legacy conversation remains exhausted.
6. Validate with a genuinely new, authorized bot request. Do not replay or reroute a blocked old reply. Verify actual handling, one publication, ACK, and persistent counters after restart.

The new root policy applies to messages admitted after enablement. Already captured work keeps its original conversation. It does not automatically retry, reclassify, or resend any previously blocked status update.

For rollback, disable future root creation and review outstanding exchanges first. Preserve the new state. An older binary does not enforce the new pair budget on retained new-root conversations; do not downgrade an active opted-in account and claim the same guarantees. Restoring a pre-migration backup can also lose newly collected updates. Use the existing deliberate receiver handoff procedure rather than copying cursors or deleting tables.

## Offline verification

`tests/test_bot_exchanges.py` covers legacy schema migration without counter changes, a fresh independent exchange after legacy exhaustion, permanent reply lineage, edits across policy disable, unknown replies and edits, a storm of fresh roots, rolling boundary behavior, restart and toggle persistence, uncertain/rejected sends, idempotency, concurrent last-slot allocation, sender/destination isolation, human-started lineage, reply budget borrowing, clock rollback and explicit CLI controls.

These checks use temporary state and `FakeTransport`. They do not establish live activation, cross-version operation, or compatibility with a peer that deliberately drops reply lineage. Keep live activation separate from this proposal.
