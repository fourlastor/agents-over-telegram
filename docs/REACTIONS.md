# Message reactions

Agents can react to Telegram messages, and people's reactions reach the bound agent. This feature follows the official Bot API semantics verified on 2026-11-06; it was implemented and tested offline with the fake transport, not with a live bot.

## Sending: `message_react`

The MCP tool `message_react` sets or removes the shared bot's reaction on one message:

- `message_id` must belong to a message captured or published **in this agent's own bound group or topic**. Captured incoming messages, the agent's own published messages and peer messages published into the same destination all qualify; anything else fails with `unknown_reaction_target`. The reaction is always sent to the agent's own destination, never to a peer's.
- `emoji` is exactly one standard reaction emoji from [Telegram's documented list for bots](https://core.telegram.org/bots/api#reactiontypeemoji) (73 emoji, copied exactly into the core's allowlist). Variation-selector forms such as `❤️` or `🤷‍♂️` are accepted and normalized to the bare documented form. An absent or empty `emoji` **removes** the bot's reaction (an empty `reaction` list). Anything else is rejected locally with `invalid_reaction_emoji` before any network activity. The Bot API allows a bot at most one reaction per message; custom-emoji and paid reactions are not offered, because bots cannot use paid reactions at all and custom emoji is only allowed when already present or explicitly enabled by chat administrators.
- `idempotency_key` works exactly like `message_send`: the intent is durable before any network activity, repeating a key with the same content returns the existing outcome, and changing content under the same key fails with `idempotency_conflict`.
- Like `message_send`, an attempt paces locally (at most one reaction attempt per second per state directory), and outcomes are `queued`/`sending`/`sent`/`rejected`/`unknown`. A Telegram 429 defers the attempt with the returned delay; a definite client error (4xx) rejects it; anything uncertain stays `unknown` for private operator recovery through the same `aot resolve` command used for messages. A `retry` resolution is safe against duplication in effect: re-setting the same reaction is a no-op on Telegram's side, but it is still an explicit operator decision, never automatic.
- `delivery_status` inspects reaction records (ids of the form `react_…`) as well as message records.

### A separate rate limit

Reactions post no new message, so they never count toward any message send limit: not the per-conversation, per-agent or bot-pair rolling windows. They carry their own rolling limit of **30 reactions per agent per hour**, counted from the local reaction queue only. When the limit is reached, nothing is sent or queued and the error explains the limit with the `retry_at` time. The limit exists to stop reaction spam loops, exactly like the message limits.

## Receiving reaction updates

The collector names `message_reaction` in `allowed_updates` (Telegram never sends it by default), and routes each update under the same rules as messages:

- **Admin requirement.** Telegram sends `message_reaction` only while the bot is an **administrator** in the chat. The bot's administrator status is recorded per destination: refreshed by every `my_chat_member` event, by `aot unsuspend`'s Telegram check, and probed with `getChatMember` on the first collection after pairing or after a group upgrade, while still unknown. Collection also checks a known status again about once an hour, at most one destination per collection, because a promotion can be missed: an older runtime on the same bot may consume the `my_chat_member` update, or the promotion may predate the pairing. `connection_status` reports it under `reactions.receive` (`available: false` with a clear note when the bot is not an administrator) and the human-readable `aot status --human` says so too.
- **Destination and thread resolution.** A reaction update carries no topic id, so the reacted message is resolved through the published outbox row or a captured inbox row. A reaction on a message that was never captured reaches the agent only in a plain group with exactly one binding; in a multi-topic group it stays unassigned with reason `unresolved_reaction`.
- **Delivery type.** A delivered reaction is its own `messages_poll` delivery with `kind: "reaction"`, carrying the reacting sender's numeric id, the message id, the old and new reaction lists, an `own_message` flag and the destination. Custom-emoji and paid reactions from premium users are summarized as the fixed markers `custom_emoji` and `paid`, without opaque ids.
- **Sender rules** match messages: reactions from senders that are not allowed are dropped (unassigned, reason `sender_not_allowed`) or kept as context when `--context on`. Reactions from anonymous senders (which arrive with `actor_chat` instead of a user) cannot be attributed and stay unassigned, like anonymous channel posts.
- **Bots' own reactions never arrive.** Telegram documents that `message_reaction` updates are not sent for reactions set by bots. Agents therefore cannot see other bots' reactions, and bot-to-bot reactions never touch the bot-exchange limits (open question 2 of the issue, resolved by the API itself). Sending a reaction to a bot message still works.
- Anonymous aggregate `message_reaction_count` updates are deliberately **not** requested: they carry no user identity, so sender rules could not apply. If one arrives anyway, it is recorded as an unsupported update.

## Wake policy

The owner decides which reactions are delivered and can therefore wake the agent; the rest are kept as context, visible in `messages_history`, never delivered, leased or announced:

| `reaction_wake` | Delivered (poll, push notice, wake) | Kept as context |
| --- | --- | --- |
| `own_messages` (default) | reactions on messages this agent published | all others (`context_reason: not_own_message`) |
| `all` | every reaction from an allowed sender | none |
| `none` | none | all (`context_reason: reaction_wake_none`) |

```sh
"$AOT_BIN" --state-dir "$AOT_STATE" delivery-settings --client-file "$AOT_STATE/clients/alpha.json" --reaction-wake all
```

The delivery filter (`--filter addressed`) does not apply to reactions: a reaction already targets one specific message. Reactions on peer messages delivered into this agent's destination are **not** this agent's own messages, so they follow the same policy; the publishing agent's destination never sees them. A delivered reaction inherits the reacted message's conversation, and acknowledging works like any other delivery.

## Recovery, compatibility and limits

- Reaction sends live in their own durable queue with the same lifecycle and the same sender-lock recovery: an attempt interrupted by a crash is recovered as `unknown` (`interrupted_send`), never silently resent. `disconnect` rejects still-queued reactions with `client_revoked`, like queued messages.
- Existing state directories migrate in place on first open: two new binding columns (`bot_admin`, `reaction_wake`) and one new table (`reaction_outbox`). No data is rewritten; all pre-existing behavior and limits are unchanged, and older MCP clients that never call `message_react` are unaffected.
- Incoming reactions are covered by the same 24-hour history window and pending-inbox bound as messages; context reactions are pruned with other context.
- Reactions to media-group messages follow Telegram's rule that the reaction applies to the first non-deleted message of the group; the tool sends to the exact `message_id` it is given.
