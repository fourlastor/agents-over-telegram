# Agent hosts and push delivery

Agents over Telegram (AOT) works with four agent hosts: **Codex**, **Claude Code**, **Pi** and **Amp**. Each host uses its own plugin or extension system. In every host, a new Telegram message reaches the running agent without anyone typing a prompt. An idle agent wakes up. A busy agent gets the message at its next safe point, usually after the current tool call.

This page explains how each integration works, how to set it up, and what was tested. The [evidence section](#what-was-tested) says exactly which checks used a real agent host and which were only offline tests.

## How push works

All hosts share the same parts:

- **The inbox and tools stay the same.** Each project has its own client file and inbox. The agent still reads messages with `messages_poll`, replies with `message_send` and confirms with `messages_ack`.
- **A notice, never the message.** The push is a fixed notice such as "1 new Telegram message for this agent. From: @alice (human)." It never contains the message text. The agent must poll to read the text, so leases, acknowledgements, pause and sender rules still apply.
- **One notice per message.** AOT records which deliveries were announced. A watcher and a hook never announce the same message twice.
- **Collection.** Something must fetch updates from Telegram. In Pi and Amp, a small watcher (`aot watch --collect`) runs while the host is open. In Claude Code, the same work is done by a background hook (`aot wake`) and, in interactive sessions, also by the monitor. It long-polls Telegram and announces new messages. In Codex, the hooks collect at most every 10 seconds, or the `relay-desktop` listener collects.
- **Per-project runtime.** Setup writes `.aot-runtime.json` next to `.aot-profile.json`. It names the prepared `aot` executable for that project and contains no secret. Host plugins read this file only in the exact project folder. They never search parent folders and never use an `aot` found on `PATH`.
- **Unrelated projects stay quiet.** In a folder without AOT setup, the plugins do nothing.
- **No orphan processes.** The watcher stops when the host process that started it exits, even after a forced kill.

| Host | Plugin type | Idle agent | Busy agent | Tools come from |
|---|---|---|---|---|
| Claude Code | Claude Code plugin (`.claude-plugin/`) | A background `asyncRewake` hook (`aot wake`) exits with status 2, and Claude Code wakes the session. In interactive sessions the plugin **monitor** also prints the notice | The monitor notice, or a `PostToolUse` hook, arrives after the current tool call; a `Stop` hook catches messages that arrive during the final answer | The plugin's MCP server |
| Codex | Codex plugin hooks (`codex/hooks.json`) | The existing `relay-desktop` listener wakes a desktop chat ([details](DIRECT_DISPATCH.md)) | `PostToolUse` and `Stop` hooks add the notice | `.codex/config.toml` written by setup |
| Pi | Pi extension (`integrations/pi/`) | The extension calls `pi.sendMessage(..., {triggerTurn: true})` and Pi starts a turn | Pi delivers the same message as a steering message after the current tool call | `.pi/mcp.json` written by setup |
| Amp | Amp plugin (`integrations/amp/`) | The plugin appends the notice to the current thread; it opens a thread if none exists | `appendUserMessage(..., {steer: true})` queues it as a steering message | `.amp/settings.json` written by setup |

## Set up a project

Register the bot and pair the project once, as described in [local setup](OPERATIONS.md#codex-setup). Choose the host with `--host`:

```sh
aot setup --label Alpha --project-dir /absolute/path/to/project --host claude
```

`--host` accepts `codex` (the default), `claude`, `pi` and `amp`. Setup always writes `.aot-profile.json` and `.aot-runtime.json`. It also writes the host's MCP configuration: `.codex/config.toml` for Codex, `.pi/mcp.json` for Pi and `.amp/settings.json` for Amp. Claude Code gets its MCP server from the plugin. Setup keeps unrelated settings and refuses to replace a different `telegram` entry. You can run setup again with another `--host` to use the same project from a second host.

### Claude Code

1. Add the marketplace and install the plugin for your user:

   ```sh
   claude plugin marketplace add fourlastor/agents-over-telegram
   claude plugin install agents-over-telegram@agents-over-telegram --scope user
   ```

   User scope is the default because it lets every later session start with the plugin loaded, so a project can be connected without a restart (see below). It has a cost in projects that are not set up: each session runs the small launcher as an idle MCP server and as the monitor (each checks for setup every 2 seconds), each tool use and turn end starts a short `python3` hook that exits at once, and the session's instructions include the plugin's text that Telegram is not connected yet. To keep it out of other projects, install with `--scope local` inside the project folder instead; connecting a project then needs the one `/reload-plugins` described below.
2. In the project folder, ask Claude Code to "connect this agent to Telegram", then "Telegram status".
3. Keep the session open. The plugin collects Telegram updates for this project, so you do not need a separate listener.

**Connecting without a restart.** A project can be set up and paired while a session is open. Until setup writes `.aot-runtime.json`, the plugin's MCP server answers with no tools, and its instructions say that setup is pending. The launcher checks for the pointer every 2 seconds. Once it appears, the launcher starts the runtime and sends `notifications/tools/list_changed`, and the Telegram tools appear in the session. Hooks read the pointer on every event. The wake waiter starts at the next turn end. A monitor that started before setup begins watching within seconds.

This needs the plugin to be loaded in the session. When the plugin is installed or enabled during the session (for example on first use), the user types `/reload-plugins`, and nothing else:

- In the terminal CLI, `/reload-plugins --force` loads the hooks, skills and MCP server in place. Without `--force`, Claude Code may hold back the new MCP server to protect the prompt cache.
- In the desktop app, `/reload-plugins` loads the hooks and skills, but Claude Code connects a newly added plugin MCP server only in the next session. Until then the agent uses the same tools from a shell: `aot tool messages_poll`, `aot tool message_send` and `aot tool messages_ack`, run in the project folder with JSON arguments on stdin. They apply the same validation, scoping and untrusted-content envelope as the MCP server. Claude Code hook and monitor notices name the exact `aot` executable for this fallback. Unlike MCP tools, which can be allowed one by one, a Bash permission rule for `aot tool` allows every tool, including `connection_pause` and `connection_disconnect`; approve these commands one at a time if that matters to you.

**How an idle session wakes up.** The plugin starts `aot wake` as an `asyncRewake` hook when the session starts and each time a turn ends. Claude Code runs this hook in the background. The hook waits for a new delivery and collects from Telegram while it waits. When a message arrives, the hook writes the notice and exits with status 2. Claude Code then wakes the session, even when it is idle, and shows the notice to the model.

- One waiter runs per project client and Claude Code session. Each turn end starts a new waiter, which takes over from the running one when that waiter finishes its current check, usually within 30 seconds.
- The waiter stops when Claude Code exits. It also stops quietly 23 hours after the last turn end. A session that stays idle for longer than that stops waking up until someone sends it a prompt.
- The launcher passes exit status 2 on only when the runtime printed a real notice. A runtime that predates `aot wake` fails with status 2 and a usage message, and that must not wake the session.

The plugin also declares a monitor that announces new messages. Claude Code starts plugin monitors only in **interactive** terminal sessions. They do not start in `-p` mode, and they did not start in the Claude Code desktop app. The `asyncRewake` hook does run in the desktop app.

The plugin needs `python3` 3.8 or newer on `PATH` to run the small launcher. The launcher only reads `.aot-runtime.json` and then starts the prepared runtime, which uses its own Python.

**The MCP server survives runtime changes.** Claude Code starts the plugin's MCP server once per session. The launcher stays running between Claude Code and the runtime's `aot mcp` process and passes messages through unchanged. It starts a new runtime process before the next tool call in two cases:

- `.aot-runtime.json` now names a different executable, for example after the project moved to an updated runtime.
- The runtime process stopped.

The launcher waits until no call is in flight. It replays Claude Code's own `initialize` and `notifications/initialized` messages to the new process, then sends `notifications/tools/list_changed`, so Claude Code refetches the tools. A call that was in flight when the process stopped gets an error answer instead of no answer. Hooks start a new process for every event, so they use the new runtime right away. The wake waiter and the monitor run for a long time, and they also collect from Telegram, so old code in them would route updates the old way for the whole bot. The launcher therefore starts them with `AOT_FOLLOW_RUNTIME=1`. At the end of each collection cycle they check `.aot-runtime.json`. When it names another runtime, they exit with status 3, and the launcher starts the same waiter or monitor again on the new runtime. A runtime older than this ignores the variable and keeps running until it exits by itself. A changed `hooks.json`, skill or monitor still needs `/reload-plugins` or a new session.

Claude Code also offers "channels" for chat bridges. Channels need an allowlisted plugin or the `--dangerously-load-development-channels` flag, and they do not work with `-p`. AOT uses hooks and a monitor instead, which need neither.

In `-p` (non-interactive) mode, Claude Code cancels background hooks when the run ends, so `aot wake` only covers the run itself.

### Codex

The Codex plugin ships hooks for `SessionStart`, `UserPromptSubmit`, `PostToolUse` and `Stop`. Codex runs them only after you review and approve them, in whichever Codex you use: the desktop app's hook review, or `/hooks` in the Codex CLI in the project.

**Codex asks again after every plugin update.** Codex stores two hashes for each approved plugin hook in `~/.codex/config.toml`: `trusted_hash` for the hook definition and `trusted_content_hash` for the whole plugin folder. It runs the hook only while both still match. Every AOT release changes the plugin folder, so after each update Codex stops running the hooks until you approve them again. This was read in the Codex source (`codex-rs/hooks/src/engine/discovery.rs`, `plugin_trust.rs` and `plugin_fingerprint.rs`). AOT computes the folder hash the same way. For the installed 0.9.14 plugin it gave exactly the value Codex had stored.

- `aot codex-hooks` reports `approved`, `needs_approval` (with a reason), `not_installed` or `unknown`. It only reads files.
- When the MCP server runs for a project whose `.codex/config.toml` starts it with this client, and the hooks need approval, the server adds a warning to its instructions and a `host_warning` field to `messages_poll` results. The agent then asks you to approve the hooks again. The server checks its parent processes to see which Codex started it, and names the matching steps: the desktop app or `/hooks` in the CLI. If it cannot tell, it names both. A server started by another host, such as Claude Code in a project set up for both, shows no warning. The text is fixed by AOT.

**Version 0.8.0 shipped these hooks, but Codex did not load them.** The 0.8.0 package had two manifests: a root `plugin.json` in the Agent Plugins format and `.codex-plugin/plugin.json`. Codex uses the root `plugin.json` when it exists, and Codex loads no hooks for an Agent Plugins package. So Codex reported no plugin hooks and never asked to trust them. The package now has only `.codex-plugin/plugin.json`, and Codex loads the four hooks as plugin hooks. See [how this was checked](#how-the-codex-plugin-hooks-were-checked).

If you used 0.8.0 and copied `codex/hooks.json` into a project's `.codex/hooks.json` as a workaround, delete that project file after you update the plugin. Otherwise Codex runs the hooks twice and adds every notice twice.

The hooks use the project folder from Codex's hook input (`cwd`). They collect from Telegram at most every 10 seconds. If `relay-desktop --collect` is running, the hooks see its collector lock and do not compete with it. Idle desktop chats still need `relay-desktop`, as before ([listening](DIRECT_DISPATCH.md)).

Setup also approves four tools of the project's scoped `telegram` server in `.codex/config.toml`: `messages_poll`, `message_send`, `message_react` and `messages_ack`. Without this, Codex's automatic approval reviewer (`approvals_reviewer = "auto_review"`) rejected `messages_poll` in a live run. It judged the call as drift from the user's typed task, because the request came from a hook notice. The approval covers only these four tools of this project's server. That server can read only this project's inbox and send only to its bound topic, within the existing rate and exchange limits. Pause, disconnect and connection tools still follow your normal approval policy. If the config already has a `tools` table for `telegram`, setup leaves it unchanged. Remove the four tables to require approval again.

### Pi

1. Run setup with `--host pi`. It writes `.pi/mcp.json` with `exposure: direct`, so the tools appear as `telegram/messages_poll` and so on.
2. Load the extension. For one run: `pi -e /path/to/agents-over-telegram/integrations/pi/agents-over-telegram.ts`. To keep it for the project: `pi install /path/to/agents-over-telegram/integrations/pi -l`.
3. Start Pi in the project folder and trust the project when Pi asks. Pi ignores `.pi/mcp.json` in untrusted projects.

### Amp

1. Run setup with `--host amp`. It writes `amp.mcpServers.telegram` to `.amp/settings.json`.
2. Approve the workspace server once: `amp mcp approve telegram`.
3. Copy or link `integrations/amp/agents-over-telegram.ts` into the project's `.amp/plugins/` folder. Amp loads project plugins from there. Reload with the command palette action `plugins: reload`, or restart Amp.

## What was tested

All checks below ran on macOS between 2026-11-02 and 2026-11-03 with the **fake Telegram transport**. That transport is a local JSON file: the test adds a Telegram update to it and reads back what the agent published. No bot token and no Telegram network were used. The real bots on this machine were not touched. Each run used a new isolated state folder.

`scripts/e2e_fake.py` prepares such a folder and injects messages. Each check below ran the **real host** with the **real integration** loaded, and the agent handled the message without any typed follow-up. "Pass" means the fake Telegram recorded the reply and the inbox showed zero pending messages afterwards.

| Host and version | Model | Scenario | Result |
|---|---|---|---|
| Claude Code 2.1.302, interactive, plugin loaded with `--plugin-dir` | Sonnet 5.5 | Idle session at the prompt | Pass. Reply 10 seconds after the message (earlier runs: 5–7 seconds, 3 of 3 messages). |
| Claude Code 2.1.302, interactive | Sonnet 5.5 | Busy: three `sleep 20` commands; message sent during the first | Pass after a fix (see below). Reply 16 seconds after the message, right after the first sleep. The task then continued. |
| Claude Code 2.1.302, `-p` (hooks only, separate collector loop) | Sonnet 5.5 | Busy: three `sleep 15` commands | Pass after the same fix. The `PostToolUse` hook notice arrived after the first sleep; reply 21 seconds after the message. |
| Codex CLI 0.163.0, `codex exec`, plugin hooks file loaded as a project hook (see below), hook trust bypassed for this one run | Codex default (`gpt-6.1-terra`) | Busy: three `sleep 15` commands; message sent during the first | Pass. The `PostToolUse` hook notice arrived after the first sleep. Codex polled, replied and acknowledged, then ran the other two sleeps. Reply 55 seconds after the message. |
| Pi 1.5.0, interactive, `-e` extension, `.pi/mcp.json` | `openai-codex/gpt-6.1-terra` | Idle | Pass. The extension started a turn; reply 27 seconds after the message. |
| Pi 1.5.0, interactive | `openai-codex/gpt-6.1-terra` | Busy: three `sleep 15` commands | Pass. Pi steered the notice in after the first sleep; reply 24 seconds after the message. The agent acknowledged it and finished the remaining sleeps. |
| Pi 1.5.0, interactive | Scripted local model (earlier run) | Idle and busy | Pass. Replies 1 and 4 seconds after the message. |
| Amp 0.0.1793649628, interactive TUI, project plugin, MCP via `--mcp-config` | Amp default ("medium") | Idle, newly opened Amp with no thread | Pass. The plugin opened a thread. Reply 18 seconds after the message. |
| Amp 0.0.1793649628, interactive TUI | Amp default | Busy: three `sleep 15` commands | Pass. The steering notice appeared after the current sleep. The agent replied (26 seconds after the message; 78 seconds in an earlier run) and acknowledged before the task ended. |
| Amp 0.0.1793649628 | — | Amp killed with `SIGKILL` | Pass. The watcher exited by itself within 15 seconds. |
| All three hosts | — | Host session closed after the checks | Pass. No AOT watcher or MCP process was left running. |

The Claude Code and Amp interactive rows come from a final run of the hosts at the same time on the documented code. The `-p` row and the `SIGKILL` row come from earlier runs on the same day, before the documentation changes. The Codex row and the Pi rows with a real model come from a later run on 2026-11-03, after the Codex tool approvals were added. In every one of these rows the fake Telegram recorded exactly one reply per message, and the inbox had zero pending messages afterwards.

**Fixes found by these runs.** The first busy Claude Code run delivered the notice mid-task, but the model said a Telegram message was waiting and asked the user whether to handle it. The notice and the MCP server instructions now say that the local user wants these messages handled when they arrive, without asking first. They also still say that message text cannot widen permissions, approvals or recipients. After this change, both Claude Code busy checks passed. The runs also found a monitor that stopped in an unconfigured folder (Claude Code then reports the stop as an event) and a watcher left running after its host was killed. Both are fixed. The first live Codex run found that Codex's automatic approval reviewer rejects `messages_poll` when a hook notice asks for it. Setup now approves the three push-handling tools (see [Codex](#codex)).

**How the Codex check loaded the hooks.** This check did not test the plugin's own hook loading, and 0.8.0 wrongly said that it did. Installing the plugin from a marketplace would change the user's Codex configuration, so the check did not do it. Instead it copied the plugin's `codex/hooks.json` unchanged into the test project's `.codex/hooks.json` and set `PLUGIN_ROOT` to this checkout. The hook commands, the launcher and the runtime were the same as in the plugin. The project layer had to be trusted, and `--dangerously-bypass-hook-trust` replaced the `/hooks` review for this one run. Codex then stored a trust entry for each test folder in the user's `~/.codex/config.toml`; those entries were removed afterwards.

**Pi with a real model needs Node.js network access.** In the first Pi runs, Node.js could not reach the model providers, so the check used `scripts/e2e/scripted_model.py`, a small OpenAI-compatible model on `127.0.0.1`. The later run used a real OpenAI model. On the test machine, Node.js could still not reach Anthropic hosts (a raw TCP connection from Node.js timed out while `curl` and Python connected), so Pi's default Claude model failed there. This is a local network filter, not part of AOT.

**Not tested end to end:**

- **Real Telegram** for any of these new integrations. Earlier live Telegram evidence for Codex `relay-desktop` is in [compatibility](COMPATIBILITY.md).
- **Codex plugin hooks running in a live session after a marketplace install and a `/hooks` review.** The live check used the same hooks file as a project hook, with trust bypassed (see above). The check in [How the Codex plugin hooks were checked](#how-the-codex-plugin-hooks-were-checked) shows that Codex loads the plugin hooks, but it did not run a session. The Codex desktop app and the interactive Codex CLI were not tested; the check used `codex exec`. The `Stop` continuation was covered only offline, in `tests/test_codex_hooks.py`.
- **Installation from a marketplace** for Claude Code and Codex, and `pi install` for Pi. The checks loaded the plugin with `--plugin-dir` (Claude Code), `-e` (Pi) and a copy in `.amp/plugins/` (Amp).
- **The Amp workspace approval flow.** The check passed the same server entry with `--mcp-config` instead of approving `.amp/settings.json`.
- **The Claude Code desktop app** installed from the marketplace. The live check (see [below](#claude-code-desktop-app)) copied this branch's hook file and launcher into an installed plugin.
- **An interactive terminal session with `aot wake`**, where the monitor and the waiter both run. Both use the same claim, so each delivery is still announced once.
- **Linux** for the new integrations.

### Claude Code desktop app

On 2026-11-05, a live session in the Claude Code desktop app (Code tab, Claude Code 2.1.317) showed:

- **No plugin monitor.** The AOT plugin's MCP server and hooks ran, but `ps` showed no monitor process. The app starts `claude` with `--output-format stream-json --input-format stream-json`.
- **`asyncRewake` wakes an idle session.** A project hook slept, wrote a line to a log file, printed a message to stderr and exited with status 2. The session was idle at the time. It woke and answered the message without a prompt. This worked with a 90-second wait and with an 11-minute wait under a 20-minute timeout, so a timeout longer than the 600-second default is honored.
- **A woken turn starts hooks again.** When the test hook ran on `UserPromptSubmit`, each wake started it again and the session woke every 90 seconds. `aot wake` therefore holds a lock per session, and the plugin starts it only on `SessionStart` and `Stop`.

These first checks used a test hook in a scratch project. A later check on the same day used the plugin itself with real Telegram:

- The plugin's `claude/hooks.json` and launcher from this change were copied into the plugin installed in a live desktop session. That session used a runtime built from the same change.
- The owner tagged the bot in a Telegram group while the session was idle. The waiter exited with the notice within a second, and the session woke.
- The session polled, replied in the group and acknowledged. When that turn ended, a new waiter started.

### How the Codex plugin hooks were checked

These checks ran on macOS on 2026-11-03 with Codex CLI 0.163.0. Each check copied the package to a temporary folder and started `codex app-server` with a new, empty `CODEX_HOME`, so the user's Codex configuration was not used or changed. A script then sent JSON-RPC requests: `plugin/read` with the package's `.agents/plugins/marketplace.json`, then `plugin/install`, then `plugin/read` again, then `hooks/list` for an empty project folder.

| Package | Hooks reported by `plugin/read` | `hooks/list` | Skills | Onboarding skill after install |
|---|---|---|---|---|
| 0.8.0 as released (root `plugin.json` and `.codex-plugin/plugin.json`) | None | None | `telegram-coordinate`, `telegram-setup` | `telegram-setup` |
| 0.8.0 with `"hooks": "./codex/hooks.json"` added to the root manifest's `extensions.com.openai` | None | Not run | Same | Not run |
| This fix: archive from `scripts/package_local.py`, no root `plugin.json` | `postToolUse`, `sessionStart`, `userPromptSubmit`, `stop` | The same four, source `plugin`, trust status `untrusted` | Same | `telegram-setup` |

Trust status `untrusted` means that Codex knows the hooks and waits for the human to trust them. The Codex 0.163.0 source code explains the first two rows. `find_plugin_manifest_path` chooses a root `plugin.json` with an Agent Plugins `$schema` before `.codex-plugin/plugin.json`. The plugin loader and `plugin/read` then skip hooks for every Agent Plugins package (`codex-rs/core-plugins/src/loader.rs` and `manager.rs`). A private package from `plugin-render` was also checked the same way: Codex loaded its `telegram` MCP server from `.mcp.json` and reported no hooks, as intended.

## Security notes

- Push notices contain only counts and fixed-format sender labels: `@username (human)`, `@username (bot)`, `user 123 (human)` or `a peer agent`. Usernames must match Telegram's character set; anything else falls back to the numeric ID.
- Setup approves only `messages_poll`, `message_send` and `messages_ack` on the project's scoped Codex server, so Codex's approval reviewer does not block pushed work. Hooks never send, lease or acknowledge. They exit 0 with no output on any error, so a broken setup cannot block the host. The Codex hook may collect from Telegram, at most every 10 seconds.
- A `Stop` hook continues a turn at most once in a row (`stop_hook_active`), so it cannot loop.
- Pi and Amp receive the notice as a user message from the plugin. Claude Code receives it as a monitor event or as hook context. In all cases the text is generated by AOT, not taken from Telegram.
- The launcher and the Pi and Amp plugins accept `.aot-runtime.json` only if it is a regular file owned by the user and not writable by group or others. The same applies to `.aot-profile.json`.
