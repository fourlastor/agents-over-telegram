# Listen with the normally opened desktop

Open ChatGPT from the app list as usual. The optional `relay-desktop` listener can wake an explicitly mapped **existing local Codex chat** through the installed native Codex queue and the app's wake URL. It needs no relay model, special desktop launch command, wrapper, replacement backend or WebSocket endpoint.

This route supports Linux and macOS. It checks the installed Codex queue capabilities instead of requiring one exact version string. The recorded live test used bundled Codex **0.163.0-alpha.10.2**; other capable builds remain eligible but have not been live-validated here. The live `relay-desktop` test passed on the normally launched desktop: marker `normal-idle-1` triggered collection, native queueing and automatic wake of the existing local Alpha chat, followed by one native poll, one reply and acknowledgment after the publication was recorded as sent. Alpha returned to zero pending messages. No model forwarded the wake after the Telegram marker arrived. Native tool and ledger evidence confirm this round trip; the user also confirmed the reply is visible in Telegram. See the [recorded result](COMPATIBILITY.md#normal-launch-local-desktop-boundary) for message/turn receipts and validation scope.

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

On Linux the native binary defaults to `/usr/lib/chatgpt/resources/codex`. On macOS it is discovered inside `/Applications/ChatGPT.app`, `/Applications/Codex.app`, or the corresponding user Applications directory: either `Contents/Resources/codex-cli/CodexCLI.app/Contents/MacOS/codex` or `Contents/Resources/codex`. `--codex-binary` selects an explicit installed bundled binary; a standalone PATH CLI or wrapper is not accepted as the backend. `--codex-home` defaults to the current `CODEX_HOME` or `~/.codex` and must identify the same native home used by the desktop. Do not point it at a copied home or a second backend. Before submitting, the adapter runs the read-only `codex queue --help` check and requires `--thread` and `--message`. A new version string alone does not block the listener. Process ownership, the expected local data layout and exact target/project checks remain required; CLI help support does not establish complete desktop wake behavior on an untested build.

`--collect` opts into intake through the existing shared collector, lock and durable cursor. Without it, the listener watches only already-captured local inbox work. Neither mode leases or acknowledges deliveries, flushes the outbox or sends Telegram replies. The target handles its own inbox with its scoped tools under the existing communication authorization.

Honor the user's requested lifecycle. “Leave it running,” “always listen,” and “restart after login” request continuous operation: use `--duration 0`. Use one 55-second check only when no ongoing lifecycle or duration was requested. A requested macOS login service is documented in [continuous listening](MACOS_SERVICE.md). A running listener waits without periodic model turns; a completed listener is no longer monitoring. Stop it with Ctrl-C or a termination signal. Stopping cannot cancel work already queued or a turn already accepted by the desktop.

The listener verifies that the desktop is running before queueing and before opening its wake URL. It waits when the app is absent rather than deliberately cold-starting it. This check and the operating-system URL opener are not atomic: if the app exits between them, the OS may launch it normally. No launcher environment or desktop configuration is changed.

### macOS process and URL checks

The outer app must identify itself as `com.openai.codex` in a protected `Info.plist`. The adapter requires trusted executable ownership/modes, one same-user app process and its direct native `app-server` child, the matching Codex home, and no external/daemon routing overrides. Darwin `libproc` supplies executable paths, parent IDs, owners and microsecond start identities; `sysctl(KERN_PROCARGS2)` supplies argv and selected routing fields only for matching app/CLI processes. Other environment fields are discarded and never logged. Inspection denial, incomplete inventory, missing processes and ambiguous backends fail closed. No Accessibility or Automation permission is needed.

The wake uses `/usr/bin/open -a <validated app bundle> codex://threads/wake/<UUID>`, pinning the app rather than relying on the default scheme handler. It retains the existing final running-process guard and the documented app-exit/opener race. Neither process discovery nor queue acceptance proves the model has handled a delivery. See [macOS validation](COMPATIBILITY.md#macos-native-queue-validation) for the measured boundary.

## Busy sessions and hooks

`codex queue` is a queue, not a promise of mid-turn steering. In the macOS live check, an accepted notice stayed queued while the target continued using tools. The desktop exposed a manual “Steer” action. Clicking it can unblock that notice, but does not validate automatic busy-session delivery.

Since version 0.8.0 the Codex plugin ships these hooks itself (`codex/hooks.json`, referenced from `.codex-plugin/plugin.json`). In 0.8.0 itself Codex did not load them, because of a root `plugin.json`; see [agent hosts](HOSTS.md#codex). They add a fixed notice at `SessionStart`, `UserPromptSubmit`, `PostToolUse` and `Stop`. The plugin's native queue remains the idle wake path. A hook does not interrupt an in-flight model request or a long-running tool. See [agent hosts](HOSTS.md#codex) for setup.

The shipped hooks follow the rules learned from the earlier operator-installed hook:

- They act only for the exact project folder from the hook input (`cwd`) and its existing connection. They respect pause, suspension and active delivery leases.
- They inject a fixed notice, never Telegram text. They never send, lease or acknowledge.
- They announce each delivery once, shared with every other push surface, and stay quiet after Telegram tool calls. A `Stop` hook continues a turn at most once in a row.
- They collect from Telegram at most every 10 seconds, because Codex has no background monitor. The shared collector lock prevents competition with a running `relay-desktop --collect`. The earlier operator hook did not collect; it relied on the listener for intake.
- They run through `scripts/aot_host_launch.py`, which only reads the project's `.aot-runtime.json` and then starts the prepared runtime. The runtime, not the system `python3`, reads the SQLite state. (An Apple Python 3.9 failed to read the live SQLite database in an earlier check.)
- The human reviews and trusts the hook definitions. `/hooks` is a **CLI** command; it was unavailable in the tested desktop UI. Do not edit trust hashes or bypass hook trust.

The earlier local macOS workaround passed a new Telegram test during an active work turn: one reply, one acknowledgment, zero pending messages, and 24.603 seconds from intake to sent receipt. That evidence applies to the operator-installed hook. The shipped hooks were tested offline against Codex's documented hook input and output, but not yet in a live Codex run; see [what was tested](HOSTS.md#what-was-tested) and the [evidence boundary](COMPATIBILITY.md#macos-follow-up-idle-and-busy-tests).

[Official Codex hook documentation](https://learn.chatgpt.com/docs/hooks) defines hook trust, `PostToolUse` output, and supported tool boundaries.

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

### A long-held unresolved wake blocks later wakes

Each client has at most one outstanding notification. While a notification is `inflight` or `uncertain`, no new wake is sent for that client, even when new Telegram messages arrive. A wake that stays unresolved therefore blocks every later wake for that client.

The relay reports this condition, but it does not fix it. When an `inflight` or `uncertain` notification is older than 15 minutes:

- `relay-status` shows it in the binding's `unresolved_wake` field.
- The client's `connection_status` shows it in `unresolved_wake`, with the notification ID, its status, the time of its last change and its `next_action`. `status --human` prints a `Wake:` line.
- `relay-desktop` writes a `desktop_wake_stuck` record with `"level": "warning"`. It checks once a minute and repeats the warning for the same notification at most once an hour. The listener keeps this limit in memory, so a restarted listener warns once again.

The `next_action` code says who must act:

| Code | Meaning | Action |
| --- | --- | --- |
| `wait_for_desktop_wake` | The native queue returned a receipt (`handoff_phase` is `queued`), but the wake URL has not succeeded yet. This happens when the desktop stays closed or the connection is paused. | Nobody resolves it. A running `relay-desktop` retries only the wake URL while the desktop is open and the connection is active. Nothing else retries it, so if no listener runs, start one. A manual `accepted` or `cancelled` would stop those retries and leave the queued notice unopened. |
| `review_failing_wake` | The same queued handoff, but its wake URL has failed 10 times (`wake_attempts`) while the desktop was open and the connection was active. The target chat may be gone, or the OS opener may always fail. The listener keeps retrying. | A human checks the target chat and the wake URL. Opening the chat in the desktop app delivers the queued notice. Then record `accepted`, or `cancelled` to dismiss it. `relay-resolve` refuses `failed`, because the desktop queue already accepted the notice. |
| `review_uncertain_wake` | Any other `uncertain` notification, or an `inflight` one. `relay-watch --once` leaves a notification `inflight` until the relay chat records a receipt, and a listener that was killed leaves it `inflight` until the next listener starts. | A human checks the target chat history and any queue receipt, then records one outcome with `relay-resolve`. A running `relay-desktop` settles an unknown desktop enqueue by itself; see below. |

In `connection_status`, these codes come after `review_unknown_delivery` and `review_intake`, and after a paused or suspended state. The `unresolved_wake` field is present in every state. The agent-facing message only asks the agent to tell the local owner; it does not name relay commands.

The relay never expires an unresolved wake by itself. It settles one case on its own: a desktop enqueue whose `codex queue` child ended without a receipt (`handoff_phase` is `enqueuing`). Two minutes after the enqueue started, a running `relay-desktop` reads the desktop's own records, read-only:

| What the records show | Outcome |
| --- | --- |
| The wake notice still waits in `queue_1.sqlite` for that chat. | The listener keeps that queue ID and retries the wake URL, as for `wait_for_desktop_wake`. |
| The chat in `thread_history_1.sqlite` holds the notice as a user message. | `accepted`. |
| Neither database has it. | `failed`. The notice never landed, so the next pass enqueues it again. |
| Either database cannot be read. | Nothing changes. The listener writes `desktop_unknown_enqueue_unsettled`, and the warning above stays. |

Only a notice created after this wake's notification counts. The Telegram message that caused the wake was collected before that time, so a turn started by such a notice reads it. A notice from an earlier wake could have been read before the message arrived, so it never counts. If the desktop moves the notice from the queue into the chat between the two reads, the listener finds neither and enqueues the notice again. The agent then gets the same fixed notice twice, which does no harm. The check assumes that only `relay-desktop` writes this notice text into a desktop chat.

Each settled case writes a `desktop_unknown_enqueue_settled` record. A Codex app restart during an enqueue is the usual cause of this case.

Any other unresolved handoff can already have reached the target, so only a human can decide what happened. For `review_uncertain_wake`, record `accepted` if the notice arrived, `failed` if it definitely did not, or `cancelled` to dismiss it. All three keep the pending inbox work. After that, the next new message can wake the client again.

## Live acceptance check

Use an authorized test target with its native Telegram tools loaded. Let it become idle, start the bounded listener, then send one unique Telegram marker to its configured destination. Confirm independently that:

- The listener selects the correct client, local UUID and exact project.
- The normally launched desktop starts that existing chat without a model forwarding the message.
- The target polls its own inbox, makes the authorized reply once and acknowledges only handled work.
- Queue/wake receipt, model completion, inbox acknowledgment and Telegram publication remain distinguishable.
- Repeated collection and a quiet interval produce no duplicate notice or reply; unrelated destinations stay isolated.

For busy-session handling, repeat with a fresh Telegram marker after independent status confirms the target is working. Record the active turn before and after intake, actual poll/reply/ack calls, and delivery latency. Do not send an inter-task reminder, press Steer, or end the work turn to make this check pass. If a hook is used, record that additional dependency and verify its execution separately.

For a persistent listener, also verify service startup, graceful-stop recovery, one collector, and retained inbox/notification state. Distinguish app restart, service restart, and OS reboot: testing one does not prove the others.

The `normal-idle-1` test passed the automatic collector → queue → local wake → poll/reply/ack flow on the existing Alpha UUID. The earlier status-only native wake probe also showed that opening the wake URL again with an empty queue produced no extra turn during the observed interval (`outputs/native-queue-live-probe.json`). These checks do not establish broader live pause/resume, busy-target, approval or app-shutdown behavior; those remain separate acceptance checks. The user confirmed the reply is visible in Telegram.

## Earlier experimental adapters

`relay-dispatch` remains an experimental option for an explicitly shared app-server endpoint. It submits a fixed **tool-output** notice, unlike the normal-launch native queue route above. Its endpoint must be the same backend that owns the desktop chat; never start a second runtime over live saved history as a substitute. It accepts a protected local Unix WebSocket or an authenticated literal-loopback WebSocket. The dispatcher does not handle the owner's approval or dynamic-tool requests.

The optional `aot-codex-bridge` adapter and native local shared-backend launch helpers require a special desktop startup process. They are retained as test infrastructure and are not the recommended plugin setup or listening route. The bridge's manually launched dispatcher passed an earlier Telegram idle-wake/reply/ack check; the normal-launch listener has its own independent live test described above. The corrected bridge child-startup gate still lacks its final real-GUI restart check.

A separate native SSH fixture passed visible idle wakeup with a canned provider. It used a different remote chat and does not establish behavior for existing local chats. The native local restart fixture demonstrated same-UUID persistence and project MCP operation, but its special startup path had coordination MCP and Artifact Session compatibility gaps. These experiments are recorded in [compatibility](COMPATIBILITY.md) and the [implementation record](IMPLEMENTATION_STATUS.md); they are not required for normal-launch listening.
