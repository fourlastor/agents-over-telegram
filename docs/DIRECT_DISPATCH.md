# Listen with the normally opened desktop

Open ChatGPT from the app list as usual. The optional `relay-desktop` listener can wake an explicitly mapped **existing local Codex chat** through the installed native Codex queue and the app's wake URL. It needs no relay model, special desktop launch command, wrapper, replacement backend or WebSocket endpoint.

This route supports Linux. It checks the installed Codex queue capabilities instead of requiring one exact version string. The recorded live test used bundled Codex **0.163.0-alpha.10.2**; other capable builds remain eligible but have not been live-validated here. The live `relay-desktop` test passed on the normally launched desktop: marker `normal-idle-1` triggered collection, native queueing and automatic wake of the existing local Alpha chat, followed by one native poll, one reply and acknowledgment after the publication was recorded as sent. Alpha returned to zero pending messages. No model forwarded the wake after the Telegram marker arrived. Native tool and ledger evidence confirm this round trip; the user also confirmed the reply is visible in Telegram. See the [recorded result](COMPATIBILITY.md#normal-launch-local-desktop-boundary) for message/turn receipts and validation scope.

```text
Telegram → shared collector → durable inbox → relay-desktop
         → native Codex queue → desktop wake URL → existing local chat
         → scoped poll / handle / reply / acknowledge
```

Setup, status and resume do not start listening. Listening is an explicit process with a chosen duration; setup does not install a service or autostart entry. ChatGPT retains its normal startup, backend, tools and approval handling.

## Select the target

Reuse the [binding procedure](RELAY.md#select-the-target): resolve the human-authorized local chat to its exact Codex thread UUID, verify its exact project directory and scoped Telegram tools, and bind the existing client to it. The project must contain the matching private profile and native Telegram MCP configuration. A Telegram message cannot select or change this mapping.

These examples use the installed `aot` executable, the account's existing canonical state root, the selected private client file and the exact project directory:

```sh
"$AOT_BIN" --state-dir "$AOT_STATE" relay-bind \
  --client-file "$AOT_CLIENT" \
  --thread-id TARGET_THREAD_UUID \
  --project-dir "$AOT_PROJECT"

"$AOT_BIN" --state-dir "$AOT_STATE" relay-status --client-file "$AOT_CLIENT"
```

Initial binding excludes existing pending work from wake notifications while leaving it available for normal polling. Use `--include-pending` only when the human wants that backlog handled. Repeating the same binding does not replay work. Do not print client capabilities or bot tokens while verifying the configuration.

The listener uses the existing notification ledger and singleton lock. Run only one of `relay-desktop`, `relay-dispatch` or `relay-watch` for a state root at a time. It checks the local target and matching project configuration before submitting; mismatches preserve the inbox and block notification.

## Run a bounded listener

Keep ChatGPT open normally and run:

```sh
"$AOT_BIN" --state-dir "$AOT_STATE" relay-desktop \
  --duration 55 --collect
```

The native binary defaults to `/usr/lib/chatgpt/resources/codex`; `--codex-binary` selects an explicit installed binary. `--codex-home` defaults to the current `CODEX_HOME` or `~/.codex` and must identify the same native home used by the desktop. Do not point it at a copied home or a second backend. Before submitting, the adapter runs the read-only `codex queue --help` check and requires `--thread` and `--message`. A new version string alone does not block the listener. Linux process ownership, the expected local data layout and exact target/project checks remain required; CLI help support does not establish complete desktop wake behavior on an untested build.

`--collect` opts into intake through the existing shared collector, lock and durable cursor. Without it, the listener watches only already-captured local inbox work. Neither mode leases or acknowledges deliveries, flushes the outbox or sends Telegram replies. The target handles its own inbox with its scoped tools under the existing communication authorization.

Honor the user's requested listening duration. If none was specified, use one 55-second check and report that limit. A running listener waits without periodic model turns; a completed listener is no longer monitoring. Stop it with Ctrl-C or a termination signal. Stopping cannot cancel work already queued or a turn already accepted by the desktop.

The listener verifies that the desktop is running before queueing and before opening its wake URL. It waits when the app is absent rather than deliberately cold-starting it. This check and the operating-system URL opener are not atomic: if the app exits between them, the OS may launch it normally. No launcher environment or desktop configuration is changed.

## Notice and recovery semantics

The adapter enqueues a fixed, trusted local notice using native `codex queue`, then opens `codex://threads/wake/THREAD_UUID` through the operating system. The native queue delivers this notice with **user-message provenance**. It is not a tool-output event. The notice contains no Telegram body, sender-supplied instructions, token or client capability; actual Telegram content is retrieved separately through the scoped inbox and remains untrusted external input.

The adapter saves its handoff phase before invoking the queue command and saves the returned queue ID before requesting the wake. A known queue ID allows a later attempt to retry **only the wake URL**, without enqueueing another notice. A queue timeout, malformed response, failed command or crash can leave an ambiguous enqueue outcome; such work is held for inspection and is never blindly enqueued again.

Inspect `relay-status` and the exact target chat when a handoff is uncertain. A notification marked `accepted` means the native queue returned success and the OS opener accepted the wake URL. It does not mean that the model completed or handled Telegram work. Native queue acceptance, a successful OS opener exit, a completed model turn, an acknowledged inbox delivery and a sent Telegram publication are different pieces of evidence. An opener exit alone does not prove the chat handled the notice. Do not resolve an uncertain enqueue as failed simply because there is no Telegram reply. Reconcile against the saved queue receipt and actual chat history before deliberately retrying or cancelling.

The target retains its model, project, permissions and native tools. The listener does not answer approvals, tool requests or authentication prompts on the target's behalf. Accepted notices are not repeated merely because inbox messages remain pending. A pause can race with an already queued notice; if that leaves pending work after resume, inspect scoped status and explicitly poll rather than duplicating the notice.

To retire a resolved mapping:

```sh
"$AOT_BIN" --state-dir "$AOT_STATE" relay-unbind --client-file "$AOT_CLIENT"
```

Unbinding stops new notifications for that mapping; it does not revoke Telegram access, delete captured context or remove an already queued native notice. Resolve outstanding handoffs first.

## Live acceptance check

Use an authorized test target with its native Telegram tools loaded. Let it become idle, start the bounded listener, then send one unique Telegram marker to its configured destination. Confirm independently that:

- The listener selects the correct client, local UUID and exact project.
- The normally launched desktop starts that existing chat without a model forwarding the message.
- The target polls its own inbox, makes the authorized reply once and acknowledges only handled work.
- Queue/wake receipt, model completion, inbox acknowledgment and Telegram publication remain distinguishable.
- Repeated collection and a quiet interval produce no duplicate notice or reply; unrelated destinations stay isolated.

The `normal-idle-1` test passed the automatic collector → queue → local wake → poll/reply/ack flow on the existing Alpha UUID. The earlier status-only native wake probe also showed that opening the wake URL again with an empty queue produced no extra turn during the observed interval (`outputs/native-queue-live-probe.json`). These checks do not establish broader live pause/resume, busy-target, approval or app-shutdown behavior; those remain separate acceptance checks. The user confirmed the reply is visible in Telegram.

## Earlier experimental adapters

`relay-dispatch` remains an experimental option for an explicitly shared app-server endpoint. It submits a fixed **tool-output** notice, unlike the normal-launch native queue route above. Its endpoint must be the same backend that owns the desktop chat; never start a second runtime over live saved history as a substitute. It accepts a protected local Unix WebSocket or an authenticated literal-loopback WebSocket. The dispatcher does not handle the owner's approval or dynamic-tool requests.

The optional `aot-codex-bridge` adapter and native local shared-backend launch helpers require a special desktop startup process. They are retained as test infrastructure and are not the recommended plugin setup or listening route. The bridge's manually launched dispatcher passed an earlier Telegram idle-wake/reply/ack check; the normal-launch listener has its own independent live test described above. The corrected bridge child-startup gate still lacks its final real-GUI restart check.

A separate native SSH fixture passed visible idle wakeup with a canned provider. It used a different remote chat and does not establish behavior for existing local chats. The native local restart fixture demonstrated same-UUID persistence and project MCP operation, but its special startup path had coordination MCP and Artifact Session compatibility gaps. These experiments are recorded in [compatibility](COMPATIBILITY.md) and the [implementation record](IMPLEMENTATION_STATUS.md); they are not required for normal-launch listening.
