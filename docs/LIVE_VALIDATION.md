# Opt-in live validation and migration

This procedure is prepared for a future explicit live request. No step here was run against the existing Remozio bot. Use the offline demo and test suite first. The private Remozio bridge, token, configuration, messages, cursor and service belong to its existing owner and must not be inspected or changed as part of this build.

## Isolated live test

Prefer an operator-owned test bot that has no existing receiver and two test groups or two forum topics. The architecture still uses one bot for both agents. Do not create a bot per agent.

1. Choose a private state directory outside any repository. Install this revision in its own virtual environment. Run `account-register` in a local terminal; enter the token only at its hidden prompt. Registration checks the bot identity and webhook metadata. The `--receiver-handoff-confirmed` flag is the operator's explicit statement that no other long-poll receiver owns this account; it is not automatic discovery of receivers on other hosts.
2. Create Alpha and Beta clients with different private capability files. Use `pair-begin` or the corresponding `connection_begin` tool for each, send its addressed code in the intended destination, and run `collect`. Review `pair-confirm --pairing-id ID` locally before using `--confirm`. Check numeric chat, topic and sender IDs. Grant only intended senders.
3. Launch two independently scoped MCP sessions. Use separate host configurations; do not give both chats a single global MCP server and claim their permissions differ. Confirm each sees only its own connection.
4. Send interleaved human messages to Alpha and Beta. Poll each, acknowledge one, restart its MCP process, and verify Beta remains pending. Repeat a poll after lease expiry and confirm replay is visible without cross-client acknowledgment. Using the same client identity after restart, call `messages_history` (or CLI `history`) and verify the captured acknowledged message remains visible within its 24-hour view. Page using the returned cursor and confirm the other client's private records are absent. Check that reading history causes no Telegram call, queued-send attempt, lease change, acknowledgment or replay.
5. Authorize one outbound message per destination. Check actual Telegram sender identity, logical-agent prefix, thread placement and numeric returned message ID. Reuse the original idempotency key and verify there is no second publication.
6. Grant Alpha-to-Beta broker access locally. Send one authorized peer message, verify a single internal delivery with origin metadata, and verify Telegram publication separately. Do not wait for a self-echo. Check the captured broker metadata and own outbound status in each authorized client's history view. Exercise a bounded reply chain, then confirm loop limits and pause stop further automation.
7. Pause Alpha, send human traffic, and verify retained pending work and Beta's independence. Resume Alpha locally, then disconnect it and verify authorization is revoked. Record remaining queue states before cleanup.
8. Inspect delivery statuses after restart and check that extended-offline warnings are understandable. Exercise uncertain-send recovery with fake transport first; do not deliberately disrupt a valuable live send to manufacture an unknown result.
9. Remove these test profiles from the host and stop their processes. Keep or explicitly delete the isolated state according to the operator's retention choice. Do not revoke the shared bot token while another client still uses it.

Use an isolated fake clock for records just inside/outside the 24-hour history boundary; do not alter the live system clock or delay live traffic to simulate this case. History validation uses messages captured during this test, not messages already consumed by another receiver or sent before binding. It does not prove complete prior-day Telegram history retrieval.

Record the git revision or source hash, OS/Python/MCP versions, destination kinds, observed permissions, message IDs, outcomes and failures. Avoid tokens, capability contents and private message bodies in the report. A successful two-topic test does not establish delivery in every Telegram privacy or administrator configuration.

## Deliberate migration of an existing receiver

This is a separate operation needing the owner of the existing integration. A local lock only coordinates this implementation's processes using the same state; it cannot prevent a different program or host from collecting the same bot.

1. Agree on a short maintenance window, the sole new collector host/state, pending-message handling, and the rollback owner. Take protected backups through the old integration's own supported procedure. This MVP does not import the old bridge's state format.
2. Stop the old receiver and disable its automatic restart through its owner. Verify it remains stopped. Drain or otherwise account for its pending inbox/outbox first; do not blindly replay old sends in the new bridge.
3. If a webhook is configured, have its owner remove it deliberately after preserving any necessary pending updates. This setup command refuses a webhook; it does not clear one. Never use `drop_pending_updates` as a default migration step.
4. Register the existing bot in the new private state with the receiver-handoff confirmation. Pair new destinations and begin one bounded collection. Check pending updates and destination mapping before enabling routine sending. Telegram may already have acknowledged earlier updates under the old receiver. The new history view contains only records captured by this bridge for its configured client; it cannot recover those consumed updates, prebinding omissions, or the old bridge's stored messages. Do not treat the 24-hour view as a migration-history import.
5. Run the isolated-test acceptance checks that apply to the chosen live destinations. Keep the old receiver stopped. Re-enable automatic activity only after its scope and delivery evidence have been reviewed.

## Rollback

Stop every new collector/MCP process and prevent further sends before starting the old receiver. Preserve the new database and review queued, sending and unknown outbox entries so rollback does not duplicate a possibly accepted publication. Reconcile any messages collected only by the new bridge through an explicit operator procedure; copying cursors or launching both receivers is not reconciliation. Restore the previous receiver configuration through its owner, then verify it is the only active receiver. Do not claim a lossless rollback: Telegram updates already acknowledged by either receiver may no longer be available upstream.
